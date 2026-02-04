#!/usr/bin/env python3
"""
Pinterest Auto-Post Script for Xotrad
WooCommerceから商品データを取得し、Pinterest API v5でピンを自動作成する。

Usage:
    python pinterest-auto-post.py                    # 未投稿商品を10件投稿
    python pinterest-auto-post.py --limit 1          # 1件だけテスト投稿
    python pinterest-auto-post.py --dry-run           # 実際に投稿せずプレビュー
    python pinterest-auto-post.py --refresh-token     # トークンをリフレッシュ
    python pinterest-auto-post.py --list-boards       # ボード一覧を表示
    python pinterest-auto-post.py --create-board      # Xotradボードを作成

Requirements:
    pip install requests woocommerce
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

import requests
from woocommerce import API

# === CONFIG ===
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

WOOCOMMERCE_CONFIG = {
    'url': 'https://xotrad.com',
    'consumer_key': 'ck_f1d34d5f51ab78f1865130dfbd2ec7796f443f44',
    'consumer_secret': 'cs_1b0cbb325b25c331c32ba8275b228747f24774f5',
}

PINTEREST_CONFIG = {
    'app_id': '1544796',
    'app_secret': '1f463402333dfdf486f23af0d1abe8bab2e506cd',
    'api_url': 'https://api.pinterest.com/v5',
    'redirect_uri': 'https://xotrad.com',
}

POSTED_FILE = os.path.join(BASE_DIR, 'pinterest-posted.json')
TOKEN_FILE = os.path.join(BASE_DIR, 'pinterest-token.json')

# Pinterest API rate limit: 50 pins per day
BATCH_DEFAULT = 20
POST_DELAY_SECONDS = 5  # delay between posts

# Default board name
DEFAULT_BOARD_NAME = 'Xotrad Collection'


# =============================================
# Token Management
# =============================================

def load_token():
    """保存済みアクセストークンを読み込む"""
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, 'r') as f:
            return json.load(f)
    return None


def save_token(token_data):
    """トークンデータをファイルに保存"""
    token_data['saved_at'] = datetime.now().isoformat()
    with open(TOKEN_FILE, 'w') as f:
        json.dump(token_data, f, indent=2)
    print(f"  トークンを保存しました: {TOKEN_FILE}")


def refresh_access_token(refresh_token):
    """
    Refresh tokenを使ってaccess tokenを更新する。
    Access token: 30日有効
    Refresh token: 60日有効
    """
    url = f"{PINTEREST_CONFIG['api_url']}/oauth/token"
    data = {
        'grant_type': 'refresh_token',
        'refresh_token': refresh_token,
    }

    print("  アクセストークンをリフレッシュ中...")
    resp = requests.post(
        url,
        data=data,
        auth=(PINTEREST_CONFIG['app_id'], PINTEREST_CONFIG['app_secret']),
        timeout=30,
    )

    if resp.status_code == 200:
        token_data = resp.json()
        print(f"  リフレッシュ成功! 有効期限: {token_data.get('expires_in', 0) // 86400}日")
        save_token(token_data)
        return token_data
    else:
        print(f"  リフレッシュ失敗: {resp.status_code} - {resp.text}")
        return None


def verify_token(access_token):
    """トークンが有効かどうか確認"""
    url = f"{PINTEREST_CONFIG['api_url']}/user_account"
    headers = {'Authorization': f'Bearer {access_token}'}

    resp = requests.get(url, headers=headers, timeout=15)
    if resp.status_code == 200:
        data = resp.json()
        print(f"  トークン有効: @{data.get('username', 'unknown')}")
        return True
    else:
        print(f"  トークン無効: {resp.status_code} - {resp.text}")
        return False


# =============================================
# Board Management
# =============================================

def list_boards(access_token):
    """ボード一覧を取得"""
    url = f"{PINTEREST_CONFIG['api_url']}/boards"
    headers = {'Authorization': f'Bearer {access_token}'}

    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code == 200:
        boards = resp.json().get('items', [])
        return boards
    else:
        print(f"  ボード取得失敗: {resp.status_code} - {resp.text}")
        return []


def find_or_create_board(access_token, board_name=DEFAULT_BOARD_NAME):
    """ボードを検索し、なければ作成する"""
    boards = list_boards(access_token)

    # Search for existing board
    for board in boards:
        if board.get('name', '').lower() == board_name.lower():
            print(f"  ボード発見: {board['name']} (ID: {board['id']})")
            return board['id']

    # Create new board
    print(f"  ボード '{board_name}' を作成中...")
    url = f"{PINTEREST_CONFIG['api_url']}/boards"
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json',
    }
    data = {
        'name': board_name,
        'description': 'Authenticated luxury ties from Japan. Curated pre-owned pieces from prestigious maisons.',
        'privacy': 'PUBLIC',
    }

    resp = requests.post(url, headers=headers, json=data, timeout=30)
    if resp.status_code in (200, 201):
        board = resp.json()
        print(f"  ボード作成成功: {board['name']} (ID: {board['id']})")
        return board['id']
    else:
        print(f"  ボード作成失敗: {resp.status_code} - {resp.text}")
        return None


# =============================================
# WooCommerce Product Fetching
# =============================================

def init_woocommerce():
    """WooCommerce API接続を初期化"""
    return API(
        url=WOOCOMMERCE_CONFIG['url'],
        consumer_key=WOOCOMMERCE_CONFIG['consumer_key'],
        consumer_secret=WOOCOMMERCE_CONFIG['consumer_secret'],
        version='wc/v3',
        timeout=120,
    )


def fetch_products(wcapi, per_page=100, max_pages=10):
    """WooCommerceから公開中の商品一覧を取得する。"""
    print("\n[1] WooCommerceから商品データ取得中...")
    all_products = []

    for page in range(1, max_pages + 1):
        params = {
            'per_page': per_page,
            'page': page,
            'status': 'publish',
            'orderby': 'date',
            'order': 'desc',
        }

        resp = wcapi.get('products', params=params)
        if resp.status_code != 200:
            print(f"  API エラー: {resp.status_code}")
            break

        products = resp.json()
        if not products:
            break

        all_products.extend(products)
        print(f"  ページ {page}: {len(products)}件取得")

        total_pages = int(resp.headers.get('X-WP-TotalPages', 1))
        if page >= total_pages:
            break

    print(f"  合計: {len(all_products)}件の商品を取得")
    return all_products


def extract_product_data(wc_product):
    """WooCommerce商品データから必要な情報を抽出"""
    images = wc_product.get('images', [])
    image_url = images[0]['src'] if images else None

    brand = ''
    for attr in wc_product.get('attributes', []):
        if attr.get('name', '').lower() == 'brand':
            options = attr.get('options', [])
            if options:
                brand = options[0]
            break

    return {
        'id': wc_product['id'],
        'sku': wc_product.get('sku', ''),
        'name': wc_product.get('name', ''),
        'price': wc_product.get('regular_price', wc_product.get('price', '')),
        'description': wc_product.get('short_description', ''),
        'permalink': wc_product.get('permalink', ''),
        'image_url': image_url,
        'brand': brand,
    }


# =============================================
# Pinterest Pin Creation
# =============================================

def generate_pin_description(product):
    """商品データからピンの説明を生成"""
    brand = product.get('brand', '')
    parts = [
        product['name'],
        '',
        f"${product.get('price', 'N/A')} USD",
        f"Brand: {brand}" if brand else '',
        'Free Worldwide Shipping from Japan',
        '',
        'Authenticated luxury from Japan.',
        '#xotrad #luxurynecktie #silktie #vintageluxury',
    ]
    if brand:
        brand_tag = re.sub(r'[^a-zA-Z0-9]', '', brand).lower()
        if brand_tag:
            parts[-1] += f' #{brand_tag}'

    return '\n'.join(p for p in parts)


def create_pin(access_token, board_id, product):
    """
    Pinterest API v5でピンを作成する。
    """
    url = f"{PINTEREST_CONFIG['api_url']}/pins"
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json',
    }

    description = generate_pin_description(product)

    data = {
        'board_id': board_id,
        'title': product['name'][:100],  # max 100 chars
        'description': description[:500],  # max 500 chars
        'link': product.get('permalink', ''),
        'media_source': {
            'source_type': 'image_url',
            'url': product['image_url'],
        },
        'alt_text': product['name'][:500],
    }

    resp = requests.post(url, headers=headers, json=data, timeout=60)

    if resp.status_code in (200, 201):
        pin = resp.json()
        pin_id = pin.get('id')
        print(f"    ピン作成成功! Pin ID: {pin_id}")
        return pin_id
    else:
        print(f"    ピン作成失敗: {resp.status_code}")
        print(f"    レスポンス: {resp.text}")
        return None


# =============================================
# Posted Record Management
# =============================================

def load_posted_records():
    """投稿済み商品の記録を読み込む"""
    if os.path.exists(POSTED_FILE):
        with open(POSTED_FILE, 'r') as f:
            return json.load(f)
    return {'posted': []}


def save_posted_record(product_id, pin_id, product_name, image_url=''):
    """投稿済み記録を保存"""
    records = load_posted_records()
    records['posted'].append({
        'product_id': product_id,
        'pin_id': pin_id,
        'product_name': product_name,
        'image_url': image_url,
        'posted_at': datetime.now().isoformat(),
    })
    with open(POSTED_FILE, 'w') as f:
        json.dump(records, f, indent=2, ensure_ascii=False)


def get_posted_product_ids():
    """投稿済み商品IDのセットを取得"""
    records = load_posted_records()
    return {r['product_id'] for r in records.get('posted', [])}


def get_posted_image_urls():
    """投稿済み画像URLのセットを取得"""
    records = load_posted_records()
    urls = set()
    for r in records.get('posted', []):
        url = r.get('image_url', '')
        if url:
            urls.add(url)
    return urls


# =============================================
# Main Flow
# =============================================

def main():
    parser = argparse.ArgumentParser(description='Pinterest Auto-Post for Xotrad')
    parser.add_argument('--limit', type=int, default=BATCH_DEFAULT,
                        help=f'投稿する商品数 (default: {BATCH_DEFAULT})')
    parser.add_argument('--dry-run', action='store_true',
                        help='投稿せずにプレビューのみ')
    parser.add_argument('--refresh-token', action='store_true',
                        help='保存済みトークンをリフレッシュ')
    parser.add_argument('--list-boards', action='store_true',
                        help='ボード一覧を表示')
    parser.add_argument('--create-board', action='store_true',
                        help='デフォルトボードを作成')
    parser.add_argument('--board', type=str, default='',
                        help='投稿先ボード名')
    args = parser.parse_args()

    print("=" * 50)
    print("Xotrad Pinterest Auto-Post")
    print("=" * 50)

    # --- Load token ---
    token_data = load_token()
    if not token_data:
        print("\n[!] トークンが見つかりません。")
        print("    まずOAuth認証を完了してください。")
        sys.exit(1)

    access_token = token_data.get('access_token')

    # --- Refresh token ---
    if args.refresh_token:
        refresh_tok = token_data.get('refresh_token')
        if refresh_tok:
            new_data = refresh_access_token(refresh_tok)
            if new_data:
                print("\nリフレッシュ完了。")
            else:
                print("\nリフレッシュ失敗。")
        else:
            print("\n[!] Refresh tokenがありません。")
        sys.exit(0)

    # --- Verify token ---
    print("\n[0] トークン確認中...")
    if not verify_token(access_token):
        print("\n[!] トークンが無効です。--refresh-token でリフレッシュしてください。")
        sys.exit(1)

    # --- List boards ---
    if args.list_boards:
        print("\nボード一覧:")
        boards = list_boards(access_token)
        for b in boards:
            print(f"  - {b['name']} (ID: {b['id']})")
        if not boards:
            print("  ボードがありません。--create-board で作成してください。")
        sys.exit(0)

    # --- Create board ---
    if args.create_board:
        find_or_create_board(access_token)
        sys.exit(0)

    # --- Find or create board ---
    board_name = args.board if args.board else DEFAULT_BOARD_NAME
    print(f"\n[1.5] ボード '{board_name}' を準備中...")
    board_id = find_or_create_board(access_token, board_name)
    if not board_id:
        print("\n[!] ボードが見つかりません。")
        sys.exit(1)

    # --- Fetch WooCommerce products ---
    wcapi = init_woocommerce()
    products_raw = fetch_products(wcapi)

    if not products_raw:
        print("\n[!] 商品が見つかりません")
        sys.exit(1)

    # Extract product data
    products = []
    for p in products_raw:
        data = extract_product_data(p)
        if data['image_url'] and data['name']:
            products.append(data)

    print(f"  画像付き商品: {len(products)}件")

    # --- Filter out already posted (IDと画像URLの両方でチェック) ---
    posted_ids = get_posted_product_ids()
    posted_urls = get_posted_image_urls()
    unposted = [p for p in products
                 if p['id'] not in posted_ids and p['image_url'] not in posted_urls]
    dup_images = len([p for p in products
                      if p['id'] not in posted_ids and p['image_url'] in posted_urls])
    print(f"  未投稿商品: {len(unposted)}件 (投稿済み: {len(posted_ids)}件, 画像重複: {dup_images}件)")

    if not unposted:
        print("\n全商品が投稿済みです。")
        sys.exit(0)

    # Apply limit
    to_post = unposted[:args.limit]
    print(f"\n[2] {len(to_post)}件の商品をピン作成します")

    # --- Create pins ---
    success_count = 0
    fail_count = 0

    for i, product in enumerate(to_post):
        print(f"\n--- ピン {i + 1}/{len(to_post)} ---")
        print(f"  商品: {product['name'][:60]}")
        print(f"  価格: ${product['price']}")
        print(f"  画像: {product['image_url'][:80]}...")

        if args.dry_run:
            desc = generate_pin_description(product)
            print(f"\n  [DRY RUN] 説明:")
            print(f"  {desc[:200]}...")
            success_count += 1
            continue

        pin_id = create_pin(access_token, board_id, product)

        if pin_id:
            save_posted_record(product['id'], pin_id, product['name'], product.get('image_url', ''))
            success_count += 1
            print(f"  ピン作成成功!")
        else:
            fail_count += 1
            print(f"  ピン作成失敗!")

        # Rate limit delay
        if i < len(to_post) - 1:
            print(f"  次のピンまで{POST_DELAY_SECONDS}秒待機...")
            time.sleep(POST_DELAY_SECONDS)

    # --- Summary ---
    print(f"\n{'=' * 50}")
    print(f"ピン作成完了!")
    print(f"  成功: {success_count}")
    print(f"  失敗: {fail_count}")
    if args.dry_run:
        print(f"  (ドライラン - 実際の投稿なし)")
    print(f"{'=' * 50}")


if __name__ == '__main__':
    main()
