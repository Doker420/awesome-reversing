import aiohttp
import uuid
import logging
from typing import Optional, Tuple
from config import (
    PLATEGA_MERCHANT_ID, PLATEGA_SECRET,
    CRYPTOBOT_API_TOKEN, WEBHOOK_URL,
    TOKEN_PLANS,
)
from database import Database

logger = logging.getLogger(__name__)

PLATEGA_API_URL = "https://app.platega.io"
CRYPTOBOT_API_URL = "https://pay.crypt.bot/api"


async def create_platega_payment(
    db: Database, user_id: int, username: str,
    plan_name: str, price: float
) -> Tuple[Optional[str], Optional[str]]:
    if not PLATEGA_MERCHANT_ID or not PLATEGA_SECRET:
        logger.error("Platega credentials not configured")
        return None, None

    plan = TOKEN_PLANS.get(plan_name, {})
    tokens = plan.get("tokens", 0)

    payload_ref = f"user_{user_id}_{uuid.uuid4().hex[:8]}"
    headers = {
        "X-MerchantId": PLATEGA_MERCHANT_ID,
        "X-Secret": PLATEGA_SECRET,
        "Content-Type": "application/json",
    }
    body = {
        "paymentDetails": {"amount": price, "currency": "RUB"},
        "description": f"LGen Tokens: {plan_name} ({tokens:,} токенов)",
        "return": WEBHOOK_URL or "https://t.me/YOUR_BOT_USERNAME",
        "failedUrl": WEBHOOK_URL or "https://t.me/YOUR_BOT_USERNAME",
        "payload": payload_ref,
        "metadata": {"userId": str(user_id), "userName": username or f"user_{user_id}"},
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{PLATEGA_API_URL}/v2/transaction/process",
                json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
            ) as response:
                data = await response.json()
                tx_id = data.get('transactionId')
                pay_url = data.get('url')
                if response.status == 200 and tx_id and pay_url:
                    await db.create_payment(user_id, price, plan_name, 'platega', str(tx_id), tokens=tokens)
                    return pay_url, str(tx_id)
                logger.error(f"Platega create failed: {data}")
    except Exception as e:
        logger.error(f"Platega API error: {e}")

    return None, None


async def create_cryptobot_payment(
    db: Database, user_id: int,
    plan_name: str, price: float
) -> Tuple[Optional[str], Optional[str]]:
    if not CRYPTOBOT_API_TOKEN:
        logger.error("CryptoBot token not configured")
        return None, None

    plan = TOKEN_PLANS.get(plan_name, {})
    tokens = plan.get("tokens", 0)

    payload_ref = f"user_{user_id}_{uuid.uuid4().hex[:8]}"
    headers = {"Crypto-Pay-API-Token": CRYPTOBOT_API_TOKEN, "Content-Type": "application/json"}
    payload = {
        "amount": str(price),
        "currency_type": "fiat",
        "fiat": "RUB",
        "accepted_assets": "USDT,TON,BTC",
        "description": f"LGen Tokens: {plan_name} ({tokens:,} токенов)",
        "payload": payload_ref,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{CRYPTOBOT_API_URL}/createInvoice",
                json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
            ) as response:
                data = await response.json()
                if data.get('ok') and data.get('result'):
                    invoice = data['result']
                    inv_id = str(invoice.get('invoice_id'))
                    pay_url = invoice.get('pay_url') or invoice.get('bot_invoice_url')
                    await db.create_payment(user_id, price, plan_name, 'cryptobot', inv_id, tokens=tokens)
                    return pay_url, inv_id
                logger.error(f"CryptoBot create failed: {data}")
    except Exception as e:
        logger.error(f"CryptoBot API error: {e}")

    return None, None


async def check_and_activate_payment(db: Database, external_id: str, method: str) -> bool:
    payment = await db.get_payment_by_external_id(external_id)
    if not payment:
        return False
    if payment['status'] == 'paid':
        return True

    is_paid = False

    try:
        async with aiohttp.ClientSession() as session:
            if method == 'platega':
                headers = {"X-MerchantId": PLATEGA_MERCHANT_ID, "X-Secret": PLATEGA_SECRET}
                async with session.get(
                    f"{PLATEGA_API_URL}/transaction/{external_id}",
                    headers=headers, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    data = await resp.json()
                    if str(data.get('status', '')).upper() == 'CONFIRMED':
                        is_paid = True

            elif method == 'cryptobot':
                headers = {"Crypto-Pay-API-Token": CRYPTOBOT_API_TOKEN}
                async with session.get(
                    f"{CRYPTOBOT_API_URL}/getInvoices?invoice_ids={external_id}",
                    headers=headers, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    data = await resp.json()
                    invoices = data.get('result', []) if data.get('ok') else []
                    if invoices and invoices[0].get('status') == 'paid':
                        is_paid = True
    except Exception as e:
        logger.error(f"Payment check error ({method}): {e}")
        return False

    if is_paid:
        tokens = payment.get('tokens', 0)
        if not tokens:
            # Фолбек: берём из TOKEN_PLANS
            plan = TOKEN_PLANS.get(payment.get('plan', ''), {})
            tokens = plan.get('tokens', 0)

        if tokens > 0:
            await db.add_tokens(payment['user_id'], tokens)

        await db.update_payment_status(external_id, 'paid')
        logger.info(f"Payment {external_id} activated for user {payment['user_id']}: +{tokens:,} tokens")
        return True

    return False