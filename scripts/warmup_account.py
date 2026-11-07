"""Manual maintenance entry point; automatic warmup runs in a runtime child."""
import argparse
import json
from backend.warmup import build_frames, warmup

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('account_id');parser.add_argument('--bars',type=int)
    args=parser.parse_args();print(json.dumps(warmup(args.account_id,args.bars),ensure_ascii=False))
