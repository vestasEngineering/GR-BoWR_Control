import traceback
import asyncio
import signal
from grbowr import GrBowr

def setup_signal_handlers(grbowr_instance):
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(
        signal.SIGINT,
        lambda: asyncio.create_task(grbowr_instance.teardown())
    )

async def main():
    grbowr = GrBowr()
    setup_signal_handlers(grbowr)
    await grbowr.main()

if __name__ == "__main__":
    try:
        asyncio.run(main(), debug=False)
    except Exception as e:
        print(e.__class__.__name__)
        traceback.print_exc()
