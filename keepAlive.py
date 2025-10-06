import os
import logging
import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def keep_alive(url: str, scheduler: AsyncIOScheduler):
    """
    Schedules a cron job to send a GET request to the specified URL every 2 minutes.
    
    Args:
        url (str): The URL to ping (e.g., http://0.0.0.0:5000/health).
        scheduler (AsyncIOScheduler): The scheduler instance to use for scheduling.
    """
    async def ping_server():
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        logger.info(f"✅ Keep-alive ping to {url} successful: {response.status}")
                    else:
                        logger.error(f"❌ Keep-alive ping to {url} failed: {response.status}")
        except Exception as e:
            logger.error(f"❌ Error during keep-alive ping to {url}: {e}")

    # Schedule the ping every 2 minutes
    scheduler.add_job(
        ping_server,
        trigger="cron",
        minute="*/2",  # Every 2 minutes
        id="keep_alive_job",
        replace_existing=True
    )
    logger.info(f"📅 Keep-alive job scheduled for {url} every 2 minutes")