import asyncio
from rich.console import Console

async def task(name, delay):
    print(f"{name} started")
    await asyncio.sleep(delay) #Sleep in this proc. Free to run task("B")
    print(f"{name} finished")
    console = Console()
    console.print(
    f"\n[bold green]Ingestion complete![/bold green]\n"
    f"  File      : \n"
    f"  Document  : :s"
)


async def main():
    await asyncio.gather(
        task("A", 2),
        task("B", 2),
        task("C", 2),
    )

asyncio.run(main()) #starts event loop

