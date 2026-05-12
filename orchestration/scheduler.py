"""定时调度模块"""
import time
import logging
import schedule

logger = logging.getLogger(__name__)


def start_scheduler(run_func, target_time: str = "14:29"):
    """启动定时器，每天target_time执行run_func"""
    schedule.every().day.at(target_time).do(run_func)
    logger.info(f"定时器已设置: 每日 {target_time}")

    while True:
        schedule.run_pending()
        time.sleep(30)
