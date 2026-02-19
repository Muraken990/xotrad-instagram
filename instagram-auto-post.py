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
    'app_id': '1210595667861115',
    'app_secret': 'd27c4befd1db45740093c4dc501e7388',
    'user_id': '17841480619602761',
    'api_version': 'v21.0',
    'graph_url': 'https://graph.instagram.com',
}

FACEBOOK_CONFIG = {
    'page_id': '1012229308632138',
    'api_version': 'v21.0',
    'graph_url': 'https://graph.facebook.com',
}

POSTED_FILE = os.path.join(BASE_DIR, 'instagram-posted.json')
TOKEN_FILE = os.path.join(BASE_DIR, 'instagram-token.json')
FACEBOOK_TOKEN_FILE = os.path.join(BASE_DIR, 'facebook-token.json')

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
# Facebook Token Management
# =============================================

def load_fb_token():
    """Facebook Page Access Tokenを読み込む（環境変数 > ファイル）"""
    env_token = os.environ.get('FACEBOOK_PAGE_ACCESS_TOKEN')
    if env_token:
        return env_token
    if os.path.exists(FACEBOOK_TOKEN_FILE):
        with open(FACEBOOK_TOKEN_FILE, 'r') as f:
            data = json.load(f)
            return data.get('access_token')
    return None


def save_fb_token(access_token):
    """Facebook Page Access Tokenをファイルに保存"""
    data = {
        'access_token': access_token,
        'page_id': FACEBOOK_CONFIG['page_id'],
        'saved_at': datetime.now().isoformat(),
    }
    with open(FACEBOOK_TOKEN_FILE, 'w') as f:
        json.dump(data, f, indent=2)
    print(f"  Facebookトークンを保存しました: {FACEBOOK_TOKEN_FILE}")


def exchange_fb_long_lived_token(short_lived_user_token):
    """
    Facebook Short-lived User Token → Long-lived User Token (60日) に変換し、
    さらに Never-expiring Page Access Token を取得して保存する。
    """
    # Step 1: User Token を Long-lived に変換
    url = f"{FACEBOOK_CONFIG['graph_url']}/oauth/access_token"
    params = {
        'grant_type': 'fb_exchange_token',
        'client_id': INSTAGRAM_CONFIG['app_id'],
        'client_secret': INSTAGRAM_CONFIG['app_secret'],
        'fb_exchange_token': short_lived_user_token,
    }
    print("  Facebook User Token を Long-lived に変換中...")
    resp = requests.get(url, params=params, timeout=30)
    if resp.status_code != 200:
        print(f"  変換失敗: {resp.status_code} - {resp.text}")
        return None
    long_lived_user_token = resp.json().get('access_token')
    print("  Long-lived User Token 取得成功")

    # Step 2: Long-lived User Token から Never-expiring Page Access Token を取得
    url2 = f"{FACEBOOK_CONFIG['graph_url']}/{FACEBOOK_CONFIG['page_id']}"
    params2 = {
        'fields': 'access_token,name',
        'access_token': long_lived_user_token,
    }
    resp2 = requests.get(url2, params=params2, timeout=30)
    if resp2.status_code != 200:
        print(f"  Page Token取得失敗: {resp2.status_code} - {resp2.text}")
        return None
    page_token = resp2.json().get('access_token')
    page_name = resp2.json().get('name')
    print(f"  Never-expiring Page Token 取得成功: {page_name}")
    save_fb_token(page_token)
    return page_token


def post_photo_to_facebook(page_access_token, image_url, caption):
    """
    Facebook Pageに写真を投稿する。
    Returns: post_id (成功時) or None (失敗時)
    """
    url = f"{FACEBOOK_CONFIG['graph_url']}/{FACEBOOK_CONFIG['api_version']}/{FACEBOOK_CONFIG['page_id']}/photos"
    payload = {
        'url': image_url,
        'caption': caption,
        'access_token': page_access_token,
    }
    resp = requests.post(url, data=payload, timeout=60)
    if resp.status_code == 200:
        post_id = resp.json().get('post_id') or resp.json().get('id')
        print(f"    Facebook投稿成功! Post ID: {post_id}")
        return post_id
    else:
        print(f"    Facebook投稿失敗: {resp.status_code}")
        print(f"    レスポンス: {resp.text}")
        return None


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

    Returns: (container_id, error_code)
        container_id: 成功時はID文字列、失敗時はNone
        error_code: エラー時はエラーコード（36003=アスペクト比エラー）、成功時はNone
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
        return container_id, None
    else:
        print(f"    コンテナ作成失敗: {resp.status_code}")
        print(f"    レスポンス: {resp.text}")
        # エラーコードを抽出
        error_code = None
        try:
            error_data = resp.json()
            error_code = error_data.get('error', {}).get('code')
        except:
            pass
        return None, error_code


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

    Returns: (media_id, container_created, is_aspect_ratio_error)
        media_id: 成功時はMedia ID文字列、失敗時はNone
        container_created: コンテナ作成まで成功したらTrue
        is_aspect_ratio_error: アスペクト比エラーの場合True
    """
    if not image_urls:
        print("    画像がありません")
        return None, False, False

    if len(image_urls) == 1:
        # --- Single image post ---
        container_id, error_code = create_media_container(access_token, image_urls[0], caption=caption)
        if not container_id:
            is_aspect_error = (error_code == 36003)
            return None, False, is_aspect_error

        print("    コンテナ処理待ち...")
        if not wait_for_container(access_token, container_id):
            return None, True, False

        result = publish_media(access_token, container_id)
        return result, True, False

    # --- Carousel post (2+ images) ---
    print(f"    カルーセル投稿: {len(image_urls)}枚")

    # Step 1: Create child containers for each image
    children_ids = []
    aspect_ratio_errors = 0
    for i, img_url in enumerate(image_urls):
        print(f"    画像 {i + 1}/{len(image_urls)}...")
        child_id, error_code = create_media_container(access_token, img_url, is_carousel_item=True)
        if not child_id:
            if error_code == 36003:
                aspect_ratio_errors += 1
            print(f"    画像 {i + 1} のコンテナ作成失敗、スキップ")
            continue
        children_ids.append(child_id)
        time.sleep(1)  # brief delay between child container creation

    if len(children_ids) < 2:
        # Carousel requires at least 2 items; fall back to single if only 1 succeeded
        if len(children_ids) == 1:
            print("    カルーセルに必要な2枚未満のため通常投稿にフォールバック")
            container_id, error_code = create_media_container(access_token, image_urls[0], caption=caption)
            if not container_id:
                is_aspect_error = (error_code == 36003) or (aspect_ratio_errors == len(image_urls))
                return None, False, is_aspect_error
            print("    コンテナ処理待ち...")
            if not wait_for_container(access_token, container_id):
                return None, True, False
            result = publish_media(access_token, container_id)
            return result, True, False
        # 全画像がアスペクト比エラーの場合
        is_aspect_error = (aspect_ratio_errors == len(image_urls))
        return None, False, is_aspect_error

    # Wait for all children to be processed
    print("    子コンテナ処理待ち...")
    for child_id in children_ids:
        if not wait_for_container(access_token, child_id):
            print(f"    子コンテナ {child_id} の処理失敗")
            return None, True, False

    # Step 2: Create carousel container
    carousel_id = create_carousel_container(access_token, children_ids, caption)
    if not carousel_id:
        return None, True, False

    print("    カルーセルコンテナ処理待ち...")
    if not wait_for_container(access_token, carousel_id):
        return None, True, False

    # Step 3: Publish
    result = publish_media(access_token, carousel_id)
    return result, True, False


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
    return {'posted': [], 'failed': []}


def save_posted_record(product_id, media_id, product_name, image_url=''):
    """投稿済み記録を保存"""
    records = load_posted_records()
    records['posted'].append({
        'product_id': product_id,
        'media_id': media_id,
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


def get_failed_product_ids():
    """失敗した商品IDのセットを取得"""
    records = load_posted_records()
    return {r['product_id'] for r in records.get('failed', [])}


def save_failed_record(product_id, product_name, reason, image_url=''):
    """失敗記録を保存"""
    records = load_posted_records()
    if 'failed' not in records:
        records['failed'] = []
    # 既に記録済みならスキップ
    existing_ids = {r['product_id'] for r in records['failed']}
    if product_id in existing_ids:
        return
    records['failed'].append({
        'product_id': product_id,
        'product_name': product_name,
        'reason': reason,
        'image_url': image_url,
        'failed_at': datetime.now().isoformat(),
    })
    with open(POSTED_FILE, 'w') as f:
        json.dump(records, f, indent=2, ensure_ascii=False)


def get_posted_image_urls():
    """投稿済み画像URLのセットを取得"""
    records = load_posted_records()
    urls = set()
    for r in records.get('posted', []):
        url = r.get('image_url', '')
        if url:
            urls.add(url)
    return urls


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
    parser.add_argument('--fb-token', type=str, default='',
                        help='Facebook Page Access Tokenを保存')
    parser.add_argument('--fb-exchange-token', type=str, default='',
                        help='Facebook Short-lived User TokenをNever-expiring Page Tokenに変換')
    args = parser.parse_args()

    print("=" * 50)
    print("Xotrad Instagram Auto-Post")
    print("=" * 50)

    # --- Facebook token handling ---
    if args.fb_exchange_token:
        token = exchange_fb_long_lived_token(args.fb_exchange_token)
        if token:
            print("\nFacebook Never-expiring Page Token の取得・保存完了。")
        else:
            print("\nFacebook Token変換失敗。")
        sys.exit(0)

    if args.fb_token:
        save_fb_token(args.fb_token)
        print("Facebookトークンを保存しました。")

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

    # --- Filter out already posted and failed (IDと画像URLの両方でチェック) ---
    posted_ids = get_posted_product_ids()
    posted_urls = get_posted_image_urls()
    failed_ids = get_failed_product_ids()
    unposted = [p for p in products
                 if p['id'] not in posted_ids
                 and p['id'] not in failed_ids
                 and p['image_url'] not in posted_urls]
    dup_images = len([p for p in products
                      if p['id'] not in posted_ids and p['image_url'] in posted_urls])
    print(f"  未投稿商品: {len(unposted)}件 (投稿済み: {len(posted_ids)}件, 画像重複: {dup_images}件, 失敗済み: {len(failed_ids)}件)")

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

        media_id, container_created, is_aspect_ratio_error = post_to_instagram(access_token, image_urls, caption)

        if media_id:
            save_posted_record(product['id'], media_id, product['name'], product.get('image_url', ''))
            success_count += 1
            print(f"  投稿成功!")
            # Facebook投稿（トークンが設定されている場合）
            fb_token = load_fb_token()
            if fb_token and not args.dry_run:
                print(f"  Facebookにも投稿中...")
                post_photo_to_facebook(fb_token, product['image_url'], caption)
        elif is_aspect_ratio_error:
            # アスペクト比エラー → 永続的な問題なので失敗リストに記録
            save_failed_record(product['id'], product['name'], 'aspect_ratio_error', product.get('image_url', ''))
            fail_count += 1
            print(f"  投稿失敗（アスペクト比エラー、今後スキップ）")
        elif container_created:
            # コンテナ作成済みだがpublish失敗（403等）→ Instagramが自動公開する場合があるため記録
            save_posted_record(product['id'], 'publish_failed', product['name'], product.get('image_url', ''))
            fail_count += 1
            print(f"  投稿失敗（コンテナ作成済み、記録保存）")
            # Instagramは自動公開されるのでFacebookにも投稿
            fb_token = load_fb_token()
            if fb_token:
                print(f"  Facebookにも投稿中...")
                post_photo_to_facebook(fb_token, product['image_url'], caption)
        else:
            # コンテナ作成自体が失敗 → 投稿されていないので記録しない
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
