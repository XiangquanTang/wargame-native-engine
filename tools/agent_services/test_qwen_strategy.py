#!/usr/bin/env python3
"""Real localhost smoke test. Uses only Python standard library; bypasses proxies."""
import argparse
import json
import sys
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8766)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('port must be 1 to 65535')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = f'http://127.0.0.1:{args.port}'
    with opener.open(base + '/api/health', timeout=30) as response:
        health = json.load(response)
    print('HEALTH:', json.dumps(health, ensure_ascii=False))
    request = urllib.request.Request(base + '/api/plan', data=json.dumps({
        'messages': [
            {'role': 'system', 'content': '你是回合制仿真游戏的策略建议助手。只提出建议，不执行动作。'},
            {'role': 'user', 'content': '游戏中本回合资源有限，请用三条简短建议说明如何分配侦察、防御和补给。'},
        ], 'max_new_tokens': 128,
    }, ensure_ascii=False).encode('utf-8'), headers={'Content-Type': 'application/json'}, method='POST')
    with opener.open(request, timeout=600) as response:
        result = json.load(response)
    if not isinstance(result.get('text'), str) or not result['text'].strip():
        raise RuntimeError('The response contains no nonempty model text.')
    print('PLAN:', json.dumps(result, ensure_ascii=False, indent=2))
    print('PASS: health and a real generation response received (not a strategy-quality/schema test).')


if __name__ == '__main__':
    try:
        main()
    except urllib.error.HTTPError as exc:
        print(f'HTTP {exc.code}: {exc.read().decode("utf-8", errors="replace")}', file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f'FAIL ({type(exc).__name__}): {exc}', file=sys.stderr)
        sys.exit(1)
