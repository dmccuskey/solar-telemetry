import asyncio
import sys
from bleak import BleakClient

ADDRESS = sys.argv[1]  # the controller's Bluetooth MAC address

async def main():
    print(f"Connecting to {ADDRESS} ...")
    async with BleakClient(ADDRESS, timeout=20.0) as client:
        print("Connected:", client.is_connected)

        services = client.services
        if services is None:
            print("No services discovered.")
            return

        for svc in services:
            print(f"\nSERVICE {svc.uuid}  ({svc.description})")
            for ch in svc.characteristics:
                props = ",".join(ch.properties)
                print(f"  CHAR {ch.uuid}  props=[{props}]")
                for d in ch.descriptors:
                    print(f"    DESC {d.uuid}")

asyncio.run(main())
