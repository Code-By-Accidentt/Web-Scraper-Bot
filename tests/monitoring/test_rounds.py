import asyncio

from scraper_bot.monitoring.rounds import wait_or_stop


async def test_the_pause_between_rounds_waits_until_it_is_stopped() -> None:
    stop = asyncio.Event()
    pause = asyncio.create_task(wait_or_stop(stop, 1))
    no_pause = asyncio.create_task(wait_or_stop(stop, 0))
    await asyncio.sleep(0.01)
    assert not pause.done() and no_pause.done()  # a 1 s pause really waits, a 0 s one does not
    stop.set()
    await asyncio.sleep(0.01)
    assert pause.done()  # and it ends as soon as the monitor is asked to stop
