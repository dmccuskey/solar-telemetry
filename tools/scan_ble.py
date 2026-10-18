import asyncio
from bleak import BleakScanner

def rssi_from_adv(adv):
    # Bleak versions vary; adv may expose .rssi or include it in details
    return getattr(adv, "rssi", None)

async def main():
    found = await BleakScanner.discover(timeout=10, return_adv=True)

    rows = []
    for addr, (dev, adv) in found.items():
        rssi = rssi_from_adv(adv)
        name = dev.name or (adv.local_name if hasattr(adv, "local_name") else None) or ""
        rows.append((rssi if rssi is not None else -999, addr, name))

    for rssi, addr, name in sorted(rows, reverse=True):
        print(f"{addr}  rssi={rssi if rssi != -999 else 'NA'}  name={name}")

asyncio.run(main())
