import asyncio
import sys
import time
from datetime import datetime, timezone

from smtc_client import SmtcClient


def main() -> None:
    flt = sys.argv[1] if len(sys.argv) > 1 else "spotify"
    client = SmtcClient(flt)
    print(f"Filter aplikasi: '{flt}'  (Ctrl+C untuk berhenti)\n")
    while True:
        try:
            raw = asyncio.run(client._read())
        except Exception as exc:  # noqa: BLE001
            print("ERROR:", exc)
            time.sleep(1)
            continue
        if raw is None:
            print("Tidak ada sesi media yang cocok (Spotify belum jalan / belum memutar?)")
        else:
            st = client.to_state(raw, datetime.now(timezone.utc), time.monotonic())
            upd = raw.updated.strftime("%H:%M:%S.%f")[:-3] if raw.updated else "TIDAK VALID"
            print(f"{raw.app_id[:28]:28} {raw.status:8} start={raw.start_ms:>7} end={raw.end_ms:>7} "
                  f"pos_raw={raw.position_ms:>7} updated={upd} -> pos_est={st.progress_ms:>7} "
                  f"| {raw.artist} - {raw.title}")
        time.sleep(1)


if __name__ == "__main__":
    main()
