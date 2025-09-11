import traceback
import asyncio
import signal
from grlrr import Grlrr

def setup_signal_handlers(grlrr_instance):
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(
        signal.SIGINT,
        lambda: asyncio.create_task(grlrr_instance.teardown())
    )

async def main():
    grlrr = Grlrr()
    setup_signal_handlers(grlrr)
    await grlrr.main()

if __name__ == "__main__":
    try:
        asyncio.run(main(), debug=False)
    except Exception as e:
        print(e.__class__.__name__)
        traceback.print_exc()











