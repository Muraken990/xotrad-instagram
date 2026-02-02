#!/usr/bin/env python3
"""
Instagram Auto-Post Script for Xotrad
WooCommerceから商品データを取得し、Instagram Graph APIで自動投稿する。

Usage:
    python instagram-auto-post.py                    # 未投稿商品を5件投稿
    python instagram-auto-post.py --limit 1          # 1件だけテスト投稿
    python instagram-auto-post.py --dry-run           # 実際に投稿せずプレビュー
    python instagram-auto-post.py --refresh-token     # トークンをリフレッシュ
    python instagram-auto-post.py --token YOUR_TOKEN  # アクセストークンを指定

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

INSTAGRAM_CONFIG = {
    'app_id': '839908195748501',
    'app_secret': '53921ed00c9ecaf80d58dafc3fc8c4a3',
    'user_id': '17841480619602761',
    'api_version': 'v21.0',
    'graph_url': 'https://graph.instagram.com',
}

POSTED_FILE = os.path.join(BASE_DIR, 'instagram-posted.json')
TOKEN_FILE = os.path.join(BASE_DIR, 'instagram-token.json')

# Instagram API rate limit: max 25 posts per 24h for content publishing
BATCH_DEFAULT = 5
POST_DELAY_SECONDS = 30  # delay between posts to avoid rate limits

# === CAPTION TEMPLATE ===
CAPTION_TEMPLATE = """\u2726 {product_name}

\U0001f4b0 ${price} USD
\U0001f3f7\ufe0f {brand}
\U0001f4e6 Free Worldwide Shipping from Japan

\U0001f517 Shop: {product_url}

#xotrad #luxurynecktie #silktie #vintageluxury #{brand_hashtag}"""


# =============================================
# Token Management
# =============================================

def load_token():
    """保存済みアクセストークンを読み込む（環境変数 > ファイル）"""
    env_token = os.environ.get('INSTAGRAM_ACCESS_TOKEN')
    if env_token:
        return env_token
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, 'r') as f:
            data = json.load(f)
            return data.get('access_token')
    return None


def save_token(access_token):
    """アクセストークンをファイルに保存"""
    data = {
        'access_token': access_token,
        'saved_at': datetime.now().isoformat(),
    }
    with open(TOKEN_FILE, 'w') as f:
        json.dump(data, f, indent=2)
    print(f"  トークンを保存しました: {TOKEN_FILE}")


def exchange_for_long_lived_token(short_lived_token):
    """
    Short-lived token を Long-lived token (60日) に変換する。
    Graph API Explorer で取得したトークンは1時間有効。
    このAPIで60日有効のlong-livedトークンに交換する。
    """
    url = f"{INSTAGRAM_CONFIG['graph_url']}/access_token"
    params = {
        'grant_type': 'ig_exchange_token',
        'client_secret': INSTAGRAM_CONFIG['app_secret'],
        'access_token': short_lived_token,
    }

    print("  Short-lived → Long-lived トークン変換中...")
    resp = requests.get(url, params=params, timeout=30)

    if resp.status_code == 200:
        data = resp.json()
        long_token = data.get('access_token')
        expires_in = data.get('expires_in', 0)
        print(f"  変換成功! 有効期限: {expires_in // 86400}日")
        save_token(long_token)
        return long_token
    else:
        print(f"  変換失敗: {resp.status_code} - {resp.text}")
        return None


def refresh_long_lived_token(current_token):
    """
    Long-lived token をリフレッシュする（有効期限を延長）。
    有効期限が残り1日以上あるトークンが必要。
    """
    url = f"{INSTAGRAM_CONFIG['graph_url']}/refresh_access_token"
    params = {
        'grant_type': 'ig_refresh_token',
        'access_token': current_token,
    }

    print("  Long-lived トークンをリフレッシュ中...")
    resp = requests.get(url, params=params, timeout=30)

    if resp.status_code == 200:
        data = resp.json()
        new_token = data.get('access_token')
        expires_in = data.get('expires_in', 0)
        print(f"  リフレッシュ成功! 有効期限: {expires_in // 86400}日")
        save_token(new_token)
        return new_token
    else:
        print(f"  リフレッシュ失敗: {resp.status_code} - {resp.text}")
        return None


def verify_token(access_token):
    """トークンが有効かどうか確認"""
    url = f"{INSTAGRAM_CONFIG['graph_url']}/me"
    params = {
        'fields': 'user_id,username',
        'access_token': access_token,
    }

    resp = requests.get(url, params=params, timeout=15)
    if resp.status_code == 200:
        data = resp.json()
        print(f"  トークン有効: @{data.get('username', 'unknown')} (ID: {data.get('user_id', data.get('id'))})")
        return True
    else:
        print(f"  トークン無効: {resp.status_code} - {resp.text}")
        return False


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
    """
    WooCommerceから公開中の商品一覧を取得する。
    画像URL、商品名、価格、説明、SKU、ブランド等を含む。
    """
    print("\n[1] WooCommerceから商品データ取得中...")
    all_products = []

    for page in range(1, max_pages + 1):
        params = {
            'per_page': per_page,
            'page': page,
            'status': 'publish',
            'stock_status': 'instock',
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

        # Check if there are more pages
        total_pages = int(resp.headers.get('X-WP-TotalPages', 1))
        if page >= total_pages:
            break

    print(f"  合計: {len(all_products)}件の商品を取得")
    return all_products


def extract_product_data(wc_product):
    """WooCommerce商品データから必要な情報を抽出"""
    # Get all image URLs (max 10 for carousel)
    images = wc_product.get('images', [])
    image_urls = [img['src'] for img in images[:10]] if images else []

    # Get brand from attributes
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
        'image_url': image_urls[0] if image_urls else None,
        'image_urls': image_urls,
        'brand': brand,
    }


# =============================================
# Instagram Posting
# =============================================

def create_media_container(access_token, image_url, caption=None, is_carousel_item=False):
    """
    Instagram メディアコンテナを作成する。
    画像URLは公開アクセス可能なURLである必要がある。
    is_carousel_item=True の場合、カルーセルの子アイテムとして作成。
    """
    url = f"{INSTAGRAM_CONFIG['graph_url']}/{INSTAGRAM_CONFIG['api_version']}/{INSTAGRAM_CONFIG['user_id']}/media"

    payload = {
        'image_url': image_url,
        'access_token': access_token,
    }

    if is_carousel_item:
        payload['is_carousel_item'] = 'true'
    elif caption:
        payload['caption'] = caption

    resp = requests.post(url, data=payload, timeout=60)

    if resp.status_code == 200:
        container_id = resp.json().get('id')
        label = "子コンテナ" if is_carousel_item else "メディアコンテナ"
        print(f"    {label}作成: {container_id}")
        return container_id
    else:
        print(f"    コンテナ作成失敗: {resp.status_code}")
        print(f"    レスポンス: {resp.text}")
        return None


def create_carousel_container(access_token, children_ids, caption):
    """
    カルーセル親コンテナを作成する。
    children_ids: 子コンテナIDのリスト
    """
    url = f"{INSTAGRAM_CONFIG['graph_url']}/{INSTAGRAM_CONFIG['api_version']}/{INSTAGRAM_CONFIG['user_id']}/media"

    payload = {
        'media_type': 'CAROUSEL',
        'children': ','.join(children_ids),
        'caption': caption,
        'access_token': access_token,
    }

    resp = requests.post(url, data=payload, timeout=60)

    if resp.status_code == 200:
        container_id = resp.json().get('id')
        print(f"    カルーセルコンテナ作成: {container_id}")
        return container_id
    else:
        print(f"    カルーセルコンテナ作成失敗: {resp.status_code}")
        print(f"    レスポンス: {resp.text}")
        return None


def check_container_status(access_token, container_id):
    """メディアコンテナの処理状態を確認"""
    url = f"{INSTAGRAM_CONFIG['graph_url']}/{INSTAGRAM_CONFIG['api_version']}/{container_id}"
    params = {
        'fields': 'status_code',
        'access_token': access_token,
    }

    resp = requests.get(url, params=params, timeout=30)
    if resp.status_code == 200:
        return resp.json().get('status_code', 'UNKNOWN')
    return 'ERROR'


def wait_for_container(access_token, container_id, max_attempts=10):
    """コンテナの処理完了を待つ"""
    for attempt in range(max_attempts):
        time.sleep(3)
        status = check_container_status(access_token, container_id)
        if status == 'FINISHED':
            return True
        elif status == 'ERROR':
            print(f"    コンテナ処理エラー: {container_id}")
            return False
        if attempt > 0:
            print(f"    状態: {status} (リトライ {attempt + 1}/{max_attempts})")
    print("    タイムアウト: コンテナ処理が完了しませんでした")
    return False


def publish_media(access_token, container_id):
    """メディアコンテナを公開（実際にInstagramに投稿）する。"""
    url = f"{INSTAGRAM_CONFIG['graph_url']}/{INSTAGRAM_CONFIG['api_version']}/{INSTAGRAM_CONFIG['user_id']}/media_publish"

    payload = {
        'creation_id': container_id,
        'access_token': access_token,
    }

    resp = requests.post(url, data=payload, timeout=60)

    if resp.status_code == 200:
        media_id = resp.json().get('id')
        print(f"    投稿公開成功! Media ID: {media_id}")
        return media_id
    else:
        print(f"    投稿公開失敗: {resp.status_code}")
        print(f"    レスポンス: {resp.text}")
        return None


def post_to_instagram(access_token, image_urls, caption):
    """
    Instagramに投稿する。
    - 画像1枚: 通常投稿
    - 画像2枚以上: カルーセル投稿（最大10枚）
    """
    if not image_urls:
        print("    画像がありません")
        return None

    if len(image_urls) == 1:
        # --- Single image post ---
        container_id = create_media_container(access_token, image_urls[0], caption=caption)
        if not container_id:
            return None

        print("    コンテナ処理待ち...")
        if not wait_for_container(access_token, container_id):
            return None

        return publish_media(access_token, container_id)

    # --- Carousel post (2+ images) ---
    print(f"    カルーセル投稿: {len(image_urls)}枚")

    # Step 1: Create child containers for each image
    children_ids = []
    for i, img_url in enumerate(image_urls):
        print(f"    画像 {i + 1}/{len(image_urls)}...")
        child_id = create_media_container(access_token, img_url, is_carousel_item=True)
        if not child_id:
            print(f"    画像 {i + 1} のコンテナ作成失敗、スキップ")
            continue
        children_ids.append(child_id)
        time.sleep(1)  # brief delay between child container creation

    if len(children_ids) < 2:
        # Carousel requires at least 2 items; fall back to single if only 1 succeeded
        if len(children_ids) == 1:
            print("    カルーセルに必要な2枚未満のため通常投稿にフォールバック")
            container_id = create_media_container(access_token, image_urls[0], caption=caption)
            if not container_id:
                return None
            print("    コンテナ処理待ち...")
            if not wait_for_container(access_token, container_id):
                return None
            return publish_media(access_token, container_id)
        return None

    # Wait for all children to be processed
    print("    子コンテナ処理待ち...")
    for child_id in children_ids:
        if not wait_for_container(access_token, child_id):
            print(f"    子コンテナ {child_id} の処理失敗")
            return None

    # Step 2: Create carousel container
    carousel_id = create_carousel_container(access_token, children_ids, caption)
    if not carousel_id:
        return None

    print("    カルーセルコンテナ処理待ち...")
    if not wait_for_container(access_token, carousel_id):
        return None

    # Step 3: Publish
    return publish_media(access_token, carousel_id)


# =============================================
# Caption Generation
# =============================================

def generate_brand_hashtag(brand):
    """ブランド名からハッシュタグを生成"""
    if not brand:
        return 'luxury'
    # Remove spaces, special chars, lowercase
    tag = re.sub(r'[^a-zA-Z0-9]', '', brand).lower()
    return tag if tag else 'luxury'


def generate_caption(product):
    """商品データからInstagramキャプションを生成"""
    brand = product.get('brand', '')
    brand_hashtag = generate_brand_hashtag(brand)

    caption = CAPTION_TEMPLATE.format(
        product_name=product['name'],
        price=product.get('price', 'N/A'),
        brand=brand if brand else 'Premium Brand',
        product_url=product.get('permalink', f"https://xotrad.com/?p={product['id']}"),
        brand_hashtag=brand_hashtag,
    )

    return caption


# =============================================
# Posted Record Management
# =============================================

def load_posted_records():
    """投稿済み商品の記録を読み込む"""
    if os.path.exists(POSTED_FILE):
        with open(POSTED_FILE, 'r') as f:
            return json.load(f)
    return {'posted': []}


def save_posted_record(product_id, media_id, product_name):
    """投稿済み記録を保存"""
    records = load_posted_records()
    records['posted'].append({
        'product_id': product_id,
        'media_id': media_id,
        'product_name': product_name,
        'posted_at': datetime.now().isoformat(),
    })
    with open(POSTED_FILE, 'w') as f:
        json.dump(records, f, indent=2, ensure_ascii=False)


def get_posted_product_ids():
    """投稿済み商品IDのセットを取得"""
    records = load_posted_records()
    return {r['product_id'] for r in records.get('posted', [])}


def get_today_post_count():
    """本日の投稿数を取得"""
    records = load_posted_records()
    today = datetime.now().strftime('%Y-%m-%d')
    count = 0
    for r in records.get('posted', []):
        posted_date = r.get('posted_at', '')[:10]
        if posted_date == today:
            count += 1
    return count


DAILY_POST_LIMIT = 20


# =============================================
# Main Flow
# =============================================

def main():
    parser = argparse.ArgumentParser(description='Instagram Auto-Post for Xotrad')
    parser.add_argument('--limit', type=int, default=BATCH_DEFAULT,
                        help=f'投稿する商品数 (default: {BATCH_DEFAULT})')
    parser.add_argument('--dry-run', action='store_true',
                        help='投稿せずにプレビューのみ')
    parser.add_argument('--token', type=str, default='',
                        help='Instagram Access Token（指定すると保存される）')
    parser.add_argument('--refresh-token', action='store_true',
                        help='保存済みトークンをリフレッシュ')
    parser.add_argument('--exchange-token', type=str, default='',
                        help='Short-lived tokenをlong-livedに変換')
    args = parser.parse_args()

    print("=" * 50)
    print("Xotrad Instagram Auto-Post")
    print("=" * 50)

    # --- Token handling ---
    if args.exchange_token:
        token = exchange_for_long_lived_token(args.exchange_token)
        if not token:
            sys.exit(1)
        print("\nトークン変換完了。再度スクリプトを実行してください。")
        sys.exit(0)

    if args.token:
        save_token(args.token)
        access_token = args.token
    else:
        access_token = load_token()

    if not access_token:
        print("\n[!] アクセストークンが見つかりません。")
        print("    --token YOUR_TOKEN で指定するか、")
        print("    --exchange-token SHORT_TOKEN でlong-livedトークンに変換してください。")
        sys.exit(1)

    if args.refresh_token:
        new_token = refresh_long_lived_token(access_token)
        if new_token:
            print("\nリフレッシュ完了。")
        else:
            print("\nリフレッシュ失敗。新しいトークンを取得してください。")
        sys.exit(0)

    # --- Verify token ---
    print("\n[0] トークン確認中...")
    if not verify_token(access_token):
        print("\n[!] トークンが無効です。新しいトークンを指定してください。")
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

    # --- Check daily post limit ---
    today_count = get_today_post_count()
    print(f"  本日の投稿数: {today_count}/{DAILY_POST_LIMIT}")
    if today_count >= DAILY_POST_LIMIT:
        print(f"\n本日の投稿上限({DAILY_POST_LIMIT}件)に達しました。")
        sys.exit(0)
    remaining = DAILY_POST_LIMIT - today_count
    if args.limit > remaining:
        args.limit = remaining
        print(f"  残り投稿可能数に合わせてlimitを{remaining}件に調整")

    # --- Filter out already posted ---
    posted_ids = get_posted_product_ids()
    unposted = [p for p in products if p['id'] not in posted_ids]
    print(f"  未投稿商品: {len(unposted)}件 (投稿済み: {len(posted_ids)}件)")

    if not unposted:
        print("\n全商品が投稿済みです。")
        sys.exit(0)

    # Apply limit
    to_post = unposted[:args.limit]
    print(f"\n[2] {len(to_post)}件の商品を投稿します")

    # --- Post to Instagram ---
    success_count = 0
    fail_count = 0

    for i, product in enumerate(to_post):
        print(f"\n--- 投稿 {i + 1}/{len(to_post)} ---")
        print(f"  商品: {product['name'][:60]}")
        print(f"  価格: ${product['price']}")
        print(f"  画像: {product['image_url'][:80]}...")

        image_urls = product.get('image_urls', [])
        if not image_urls and product.get('image_url'):
            image_urls = [product['image_url']]
        print(f"  画像数: {len(image_urls)}枚")

        caption = generate_caption(product)

        if args.dry_run:
            print(f"\n  [DRY RUN] キャプション:")
            print(f"  {caption[:200]}...")
            for j, u in enumerate(image_urls):
                print(f"  画像{j+1}: {u[:80]}")
            success_count += 1
            continue

        media_id = post_to_instagram(access_token, image_urls, caption)

        if media_id:
            save_posted_record(product['id'], media_id, product['name'])
            success_count += 1
            print(f"  投稿成功!")
        else:
            fail_count += 1
            print(f"  投稿失敗!")

        # Rate limit delay
        if i < len(to_post) - 1:
            print(f"  次の投稿まで{POST_DELAY_SECONDS}秒待機...")
            time.sleep(POST_DELAY_SECONDS)

    # --- Summary ---
    print(f"\n{'=' * 50}")
    print(f"投稿完了!")
    print(f"  成功: {success_count}")
    print(f"  失敗: {fail_count}")
    if args.dry_run:
        print(f"  (ドライラン - 実際の投稿なし)")
    print(f"{'=' * 50}")


if __name__ == '__main__':
    main()
