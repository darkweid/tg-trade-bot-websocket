import logging
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from pybit.unified_trading import WebSocket, WebSocketTrading
from environs import Env

# Logging configuration
logging.basicConfig(format=("|%(asctime)s| %(levelname)s |%(message)s"), level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Loading environment variables
env = Env()
env.read_env()

BYBIT_API_KEY = env.str("API_KEY")
BYBIT_API_SECRET = env.str("API_SECRET")
TELEGRAM_TOKEN = env.str("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = env.str("TELEGRAM_CHAT_ID")
SYMBOL = env.str("SYMBOL")
TARGET_PROFIT_PERCENT = env.float("TARGET_PROFIT_PERCENT")
AMOUNT = env.float("AMOUNT")

# Initialize Telegram bot
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

# Initialize Bybit WebSocket for private and public channels
ws_public = WebSocket(testnet=True, channel_type="spot")
ws_private = WebSocketTrading(testnet=True, api_key=BYBIT_API_KEY, api_secret=BYBIT_API_SECRET)


class TradingBot:
    def __init__(self, websocket_private: WebSocketTrading, websocket_public: WebSocket):
        """Initializes the TradingBot instance with private and public WebSocket connections."""
        self.active_position = None
        self.ws_private = websocket_private
        self.ws_public = websocket_public
        self.orderbook_data = {"bid": None, "ask": None}
        self.orderbook_event = asyncio.Event()
        self.is_subscribed = False
        self.order_response_event = asyncio.Event()
        self.order_response = None

    def handle_orderbook(self, message: dict) -> None:
        """Processes the order book data received from WebSocket.

        Args:
            message (dict): Message containing order book data.
        """
        try:
            orderbook_data = message.get("data", {})
            if not orderbook_data.get('b') or not orderbook_data.get('a'):
                logger.warning("Incomplete orderbook data received")
                return

            bid = float(orderbook_data['b'][0][0])
            ask = float(orderbook_data['a'][0][0])

            self.orderbook_data = {"bid": bid, "ask": ask}
            self.orderbook_event.set()
            logger.debug(f"Orderbook data received: {self.orderbook_data}")
        except Exception as e:
            logger.error(f"Error in handle_orderbook: {e}")

    def handle_order_response(self, message: dict) -> None:
        """Handles the response to an order request.

        Args:
            message (dict): Message containing order response data.
        """
        self.order_response = message
        self.order_response_event.set()

    async def subscribe_to_orderbook(self) -> None:
        """Subscribes to the order book for market data updates."""
        if not self.is_subscribed:
            try:
                logger.info("Subscribing to orderbook...")
                self.ws_public.orderbook_stream(50, SYMBOL, self.handle_orderbook)
                self.is_subscribed = True
                logger.info(f"Subscribed to order book for {SYMBOL}")
            except Exception as e:
                logger.error(f"Error subscribing to order book: {e}")
                raise

    async def get_order_book(self) -> dict[str, None]:
        """Retrieves the current order book data.

        Returns:
            dict: Contains bid and ask prices.
        """
        return self.orderbook_data

    def calculate_target_price(self, entry_price: float) -> float:
        """Calculates the target price based on the target profit percentage.

        Args:
            entry_price (float): Entry price of the position.

        Returns:
            float: Target price to achieve the desired profit.
        """
        return entry_price * (1 + TARGET_PROFIT_PERCENT / 100)

    async def open_position(self) -> bool:
        """Opens a new trading position on Bybit.

        Returns:
            bool: True if position opened successfully, False otherwise.
        """
        try:
            await self.subscribe_to_orderbook()
            await asyncio.sleep(1)

            # Get the current order book data
            orderbook = self.orderbook_data
            if not orderbook.get('bid') or not orderbook.get('ask'):
                logger.error("Cannot open position: no orderbook data")
                return False

            # Clear the event before placing the order
            self.order_response_event.clear()

            # Place the order
            self.ws_private.place_order(
                callback=self.handle_order_response,
                category="spot",
                symbol=SYMBOL,
                side="Buy",
                orderType="MARKET",
                qty=str(AMOUNT),
                marketUnit="baseCoin"
            )

            # Wait for order response (max 10 seconds)
            try:
                await asyncio.wait_for(self.order_response_event.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                logger.error("Timeout waiting for order response")
                return False

            if self.order_response and self.order_response.get('retCode') == 0:
                entry_price = orderbook['ask']
                self.active_position = {
                    'order_id': self.order_response['data']['orderId'],
                    'symbol': SYMBOL,
                    'amount': AMOUNT,
                    'entry_price': entry_price,
                    'target_price': self.calculate_target_price(entry_price),
                }
                logger.info(f"Opened new position: {self.active_position}")
                asyncio.create_task(self.monitor_position())
                return True

            logger.error(f"Error opening position: {self.order_response}")
            return False

        except Exception as e:
            logger.error(f"Error in open_position: {e}")
            return False

    async def monitor_position(self) -> None:
        """Monitors the open position to close it upon reaching the target profit."""
        while self.active_position:
            try:
                order_book = await self.get_order_book()
                if not order_book or not order_book.get('bid'):
                    logger.warning("No orderbook data available for monitoring")
                    await asyncio.sleep(1)
                    continue

                current_price = order_book['bid']

                if current_price >= self.active_position['target_price']:
                    success = await self.close_position()
                    if success:
                        profit_percentage = (
                                ((self.orderbook_data['bid'] / self.active_position['entry_price']) - 1) * 100
                        )
                        self.active_position = None
                        await send_notification(f"✅ Position closed!\n"
                                                f"Trading Pair: {self.active_position['symbol']}\n"
                                                f"Profit Percentage: {profit_percentage:.2f}%\n"
                                                f"Entry Price: {self.active_position['entry_price']}\n"
                                                f"Target Price: {self.active_position['target_price']}\n"
                                                f"Exit Price: {self.orderbook_data['bid']}")
                        logger.info(f"Position closed successfully: {self.active_position}")

                await asyncio.sleep(0.1)

            except Exception as e:
                logger.error(f"Error in monitor_position: {e}")
                await asyncio.sleep(1)

    async def close_position(self) -> bool:
        """Closes the open position.

        Returns:
            bool: True if position closed successfully, False otherwise.
        """
        try:
            self.order_response_event.clear()

            self.ws_private.place_order(
                callback=self.handle_order_response,
                category="spot",
                symbol=SYMBOL,
                side="Sell",
                orderType="MARKET",
                qty=str(AMOUNT),
                marketUnit="baseCoin"
            )

            try:
                await asyncio.wait_for(self.order_response_event.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                logger.error("Timeout waiting for close position response")
                return False

            if self.order_response and self.order_response.get('retCode') == 0:
                logger.info(f"Position closed successfully: {self.order_response}")
                self.active_position = None
                return True

            logger.error(f"Error closing position: {self.order_response}")
            return False

        except Exception as e:
            logger.error(f"Error in close_position: {e}")
            return False

    # def shutdown(self):
    #     self.ws_public.close()
    #     self.ws_private.close()
    #     tasks = asyncio.all_tasks()
    #
    #     for task in tasks:
    #         task.cancel()


# Initialize the trading bot
trading_bot = TradingBot(ws_private, ws_public)


async def send_notification(message: str) -> None:
    """Sends a notification to Telegram.

    Sends the specified message to the Telegram chat using the bot.

    Args:
        message (str): The message to send.
    """
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
        logger.info(f"Sent notification to Telegram: {message}")
    except Exception as e:
        logger.error(f"Error while sending notification to Telegram: {e}")


# Telegram command handlers
@dp.message(Command("start"))
async def start_command(message: types.Message):
    """Handles the /start command."""
    await message.answer(
        "👋 Hello! I am a trading bot for Bybit.\n"
        "Available commands:\n"
        "/trade - open a new position\n"
        "/status - check the current position"
    )


@dp.message(Command("status"))
async def status_command(message: types.Message):
    """Handles the /status command."""
    try:
        if trading_bot.active_position:
            order_book = await trading_bot.get_order_book()
            if not order_book or not order_book.get('bid'):
                await message.answer("❌ Cannot get current price")
                return

            current_price = order_book['bid']
            current_profit = ((current_price / trading_bot.active_position['entry_price']) - 1) * 100

            await message.answer(
                f"📊 Current position:\n"
                f"Trading Pair: {trading_bot.active_position['symbol']}\n"
                f"Entry Price: {trading_bot.active_position['entry_price']}\n"
                f"Target Price: {trading_bot.active_position['target_price']}\n"
                f"Current Price: {current_price}\n"
                f"Profit: {current_profit:.2f}%"
            )
        else:
            await message.answer("⚠️ No open position")

    except Exception as e:
        logger.error(f"Error in status_command: {e}")
        await message.answer("❌ An error occurred while checking the position status")


@dp.message(Command("trade"))
async def trade_command(message: types.Message):
    """Handles the /trade command."""
    try:
        if trading_bot.active_position:
            await message.answer("⚠️ A position is already open")
            return

        await message.answer("⏳ Opening a position, please wait...")
        success = await trading_bot.open_position()
        if success:
            await message.answer(
                f"✅ Position opened!\n"
                f"Trading Pair: {trading_bot.active_position['symbol']}\n"
                f"Entry Price: {trading_bot.active_position['entry_price']:.2f}\n"
                f"Target Price: {trading_bot.active_position['target_price']:.2f}"
            )
        else:
            await message.answer("❌ Could not open the position")

    except Exception as e:
        logger.error(f"Error in trade_command: {e}")
        await message.answer("❌ An error occurred while opening a position")


async def main():
    """Starts the bot and sets up the event loop."""
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
