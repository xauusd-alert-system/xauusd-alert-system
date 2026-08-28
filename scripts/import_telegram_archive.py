"""Import Telegram HTML as an unlinked descriptive dataset (never computes WR)."""
import argparse

from data.channel_archive import import_archive


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("html", nargs="+")
    p.add_argument("--db-path", default="data/channel_archive.sqlite")
    args = p.parse_args(argv)
    for path in args.html:
        print(f"{path}: {import_archive(args.db_path, path)} new unlinked messages")
    print("No performance metrics computed. Link to immutable signal/broker events first.")


if __name__ == "__main__": main()
