"""Deployment management. No credentials or customer data are printed."""
import argparse
import os
from .database import Database


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=['migrate','check'])
    args=parser.parse_args()
    url=os.environ.get('DATABASE_URL')
    if not url:raise SystemExit('DATABASE_URL must be configured in the hosting service.')
    db=Database(url)
    try:
        if args.command=='migrate':db.migrate()
        if not db.ready():raise SystemExit('Schema readiness failed.')
        print('PostgreSQL schema version 4 is ready. No credential values printed.')
    finally:db.close()


if __name__=='__main__':main()
