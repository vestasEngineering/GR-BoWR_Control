
import asyncio
import traceback

event_loop = None

def setup():
    event_loop = asyncio.get_running_loop()
    event_loop.create_task()    


async def loop():
    while True:
        print('main loop')
        start = event_loop.time()
        await asyncio.sleep(1)
        print('duration: ', str(event_loop.time()-start))


async def main():
    setup()
    await loop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(e.__class__.__name__)
        traceback.print_exc()