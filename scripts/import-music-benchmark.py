"""Import completed benchmark rounds into the configured VideoDream database."""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.infrastructure.database import database
from backend.modules.music.benchmark_logs import save_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('file', type=Path)
    parser.add_argument('--benchmark-id', required=True)
    parser.add_argument('--prefix', default='')
    parser.add_argument('--provider-concurrency', type=int, default=2)
    args = parser.parse_args()
    if args.file.stat().st_size > 32 * 1024 * 1024:
        parser.error('Snapshot must not exceed 32 MiB')
    document = json.loads(args.file.read_text())
    with database() as cursor:
        inserted = save_snapshot(cursor, document, args.benchmark_id, prefix=args.prefix,
                                 provider_concurrency=args.provider_concurrency)
    print(f'Imported {inserted} new log rows; benchmark_id={args.benchmark_id}')


if __name__ == '__main__':
    main()
