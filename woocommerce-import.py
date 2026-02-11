#!/usr/bin/env python3
"""
WooCommerce Product Import Script (統合版)
処理済み画像をアップロードし、商品を登録します。

Requirements:
    pip install woocommerce requests

Usage:
    python woocommerce-import.py
    python woocommerce-import.py --limit 10  # テスト用

Input:
    - mercari_product_details_en.csv（商品データ）
    - image-processor/processed/（処理済み画像）

Output:
    - Xotrad に商品登録
    - xotrad_image_urls.csv（SKU → 画像URL マッピング）
"""

import csv
import os
import sys
import time
import argparse
import math
import re
import subprocess
from datetime import datetime
from glob import glob
import requests
from dotenv import load_dotenv
from woocommerce import API

# .env読み込み
load_dotenv(os.path.join(os.path.dirname(__file__), 'image-processor', '.env'))

# === CONFIG ===
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG = {
    'url': 'https://xotrad.com',
    'consumer_key': 'ck_f1d34d5f51ab78f1865130dfbd2ec7796f443f44',
    'consumer_secret': 'cs_1b0cbb325b25c331c32ba8275b228747f24774f5',
    'csv_file': os.path.join(BASE_DIR, 'mercari_product_details_en.csv'),
    'processed_images_dir': os.path.join(BASE_DIR, 'image-processor', 'processed'),
    'output_urls_file': os.path.join(BASE_DIR, 'xotrad_image_urls.csv'),
    'batch_size': 2,
    'delay_between_batches': 3,
    'currency': 'USD',
    # SSH/rsync設定（Xotrad）
    'ssh_host': 'ssh.lolipop.jp',
    'ssh_user': 'peewee.jp-soft-moji-2724',
    'ssh_port': 2222,
    'ssh_key': os.path.expanduser('~/.ssh/lolipop'),
    'ssh_remote_dir': 'web/xotrad/wp-content/uploads/products',
    'image_base_url': 'https://xotrad.com/wp-content/uploads/products',
}

# === 価格計算パラメータ（merucari-04-translate-optimized-part02.py と同じ） ===
PRICING = {
    'shipping_cost': 1500,      # 送料固定1,500円
    'fee_rate': 0.16,           # eBay(13.25%) + Payoneer(2%) + バッファ(0.75%)
    'max_discount': 0.10,       # Best Offer最大割引
    'min_profit': 1000,         # 最低利益1,000円
    'max_profit': 5000,         # 最大利益5,000円
    'profit_ratio': 0.5,        # 仕入れ値の50%を利益に
    'ddp_markup': 1.20,         # 20%上乗せ（関税・送料カバー、Free Shipping用）
}

# Condition mapping
CONDITION_MAP = {
    'No noticeable scratches or stains': 'Excellent',
    'Minor signs of use': 'Very Good',
    'Slight scratches or stains': 'Good',
    'Some scratches or stains': 'Fair',
    'New': 'New',
    'Like New': 'Like New',
    'Pre-owned - Excellent': 'Excellent',
}


def get_usd_jpy_rate():
    """最新のドル円レート取得"""
    try:
        response = requests.get("https://api.exchangerate-api.com/v4/latest/JPY", timeout=10)
        data = response.json()
        return round(data["rates"]["USD"], 4)
    except Exception as e:
        print(f"  為替レート取得エラー: {e}")
        return 0.0067  # デフォルト値


def calculate_price_usd(mercari_price_jpy, jpy_to_usd_rate):
    """USD価格を計算"""
    take_rate = (1 - PRICING['max_discount']) * (1 - PRICING['fee_rate'])
    profit = min(max(mercari_price_jpy * PRICING['profit_ratio'], PRICING['min_profit']), PRICING['max_profit'])
    cost = mercari_price_jpy + PRICING['shipping_cost']
    min_price_jpy = (cost + profit) / take_rate
    min_price_usd = min_price_jpy * jpy_to_usd_rate
    return math.ceil(min_price_usd * PRICING['ddp_markup'])


def parse_price_jpy(price_str):
    """日本円価格文字列をパース"""
    if not price_str:
        return 0
    price = re.sub(r'[¥,\s]', '', str(price_str))
    match = re.search(r'\d+', price)
    if match:
        return int(match.group())
    return 0


def init_api():
    """Initialize WooCommerce API connection."""
    return API(
        url=CONFIG['url'],
        consumer_key=CONFIG['consumer_key'],
        consumer_secret=CONFIG['consumer_secret'],
        version='wc/v3',
        timeout=120,
    )


def upload_images_via_rsync():
    """
    rsyncで全処理済み画像をXotradにアップロード
    """
    local_dir = CONFIG['processed_images_dir'] + '/'
    remote = f"{CONFIG['ssh_user']}@{CONFIG['ssh_host']}:{CONFIG['ssh_remote_dir']}"

    print(f"\n[2] rsyncで画像を一括アップロード中...")
    print(f"  ローカル: {local_dir}")
    print(f"  リモート: {remote}")

    # rsyncコマンド構築
    cmd = [
        'rsync',
        '-avz',                          # archive, verbose, compress
        '--progress',                    # 進捗表示
        '-e', f'ssh -p {CONFIG["ssh_port"]} -i {CONFIG["ssh_key"]}',  # SSHポート・鍵指定
        local_dir,
        remote
    ]

    print(f"  コマンド: {' '.join(cmd)}")
    print("-" * 50)

    try:
        result = subprocess.run(cmd, capture_output=False, text=True)

        if result.returncode == 0:
            print("-" * 50)
            print("  rsync完了!")
            return True
        else:
            print(f"  rsyncエラー: 終了コード {result.returncode}")
            return False

    except Exception as e:
        print(f"  rsyncエラー: {e}")
        return False


def get_processed_images_for_sku(sku):
    """処理済み画像のパスを取得"""
    pattern = os.path.join(CONFIG['processed_images_dir'], f"{sku}_*.jpg")
    images = sorted(glob(pattern))

    # PNG も確認
    if not images:
        pattern = os.path.join(CONFIG['processed_images_dir'], f"{sku}_*.png")
        images = sorted(glob(pattern))

    return images[:5]  # 最大5枚


def upload_images_for_products(products, wcapi):
    """rsyncで全画像をアップロードし、URLマッピングを作成"""

    # rsyncで一括アップロード
    success = upload_images_via_rsync()

    if not success:
        print("  ! rsyncに失敗しました")
        return {}

    # URLマッピングを作成
    print("\n[3] 画像URLマッピングを作成中...")
    image_url_map = {}  # SKU -> [url1, url2, ...]
    total_images = 0

    for i, product in enumerate(products):
        sku = product['sku']
        images = get_processed_images_for_sku(sku)

        if not images:
            image_url_map[sku] = []
            continue

        # ローカルファイル名からURLを生成
        urls = []
        for img_path in images:
            filename = os.path.basename(img_path)
            url = f"{CONFIG['image_base_url']}/{filename}"
            urls.append(url)
            total_images += 1

        image_url_map[sku] = urls

    print(f"  マッピング完了: {len(image_url_map)}商品、{total_images}枚")

    return image_url_map


def save_image_urls(image_url_map, output_file):
    """画像URLマッピングをCSVに保存"""
    print(f"\n[4] 画像URLを保存中: {output_file}")

    with open(output_file, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['SKU', 'image_url_1', 'image_url_2', 'image_url_3', 'image_url_4', 'image_url_5'])

        for sku, urls in image_url_map.items():
            row = [sku] + urls + [''] * (5 - len(urls))
            writer.writerow(row[:6])

    print(f"  保存完了: {len(image_url_map)}件")


def map_condition(condition_str):
    """Map condition string to simplified condition."""
    if not condition_str:
        return 'Good'

    first_line = condition_str.split('\n')[0].strip()

    for key, value in CONDITION_MAP.items():
        if key.lower() in first_line.lower():
            return value

    return 'Good'


def load_products_from_csv(csv_file, jpy_to_usd_rate, limit=0):
    """CSVから商品データを読み込み"""
    print(f"\n[1] 商品データを読み込み中: {csv_file}")

    products = []

    with open(csv_file, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            sku = row.get('SKU', '').strip()
            if not sku:
                continue

            name = row.get('商品名_en', '').strip()
            brand = row.get('ブランド_en', '').strip()
            condition_raw = row.get('コンディション_en', '').strip()
            description = row.get('商品説明_en', '').strip()
            color = row.get('色_en', '').strip()
            price_jpy = parse_price_jpy(row.get('価格', '0'))

            # USD価格を計算
            price_usd = calculate_price_usd(price_jpy, jpy_to_usd_rate)

            if price_usd == 0:
                continue

            condition = map_condition(condition_raw)

            products.append({
                'sku': sku,
                'name': name if name else f"{brand} Item",
                'brand': brand,
                'condition': condition,
                'description': description,
                'color': color,
                'price_jpy': price_jpy,
                'price_usd': price_usd,
            })

    if limit > 0:
        products = products[:limit]

    print(f"  読み込み完了: {len(products)}件")
    return products


def create_product_attributes(wcapi):
    """Create WooCommerce product attributes for brand and condition."""
    response = wcapi.get('products/attributes')
    existing = {a['slug']: a['id'] for a in response.json()} if response.status_code == 200 else {}

    attrs = {}

    # Create Brand attribute
    if 'brand' not in existing:
        data = {'name': 'Brand', 'slug': 'brand', 'type': 'select', 'order_by': 'name'}
        resp = wcapi.post('products/attributes', data)
        if resp.status_code == 201:
            attrs['brand'] = resp.json()['id']
            print(f"  Created Brand attribute (ID: {attrs['brand']})")
    else:
        attrs['brand'] = existing['brand']
        print(f"  Brand attribute exists (ID: {attrs['brand']})")

    # Create Condition attribute
    if 'condition' not in existing:
        data = {'name': 'Condition', 'slug': 'condition', 'type': 'select', 'order_by': 'name'}
        resp = wcapi.post('products/attributes', data)
        if resp.status_code == 201:
            attrs['condition'] = resp.json()['id']
            print(f"  Created Condition attribute (ID: {attrs['condition']})")
    else:
        attrs['condition'] = existing['condition']
        print(f"  Condition attribute exists (ID: {attrs['condition']})")

    return attrs


def create_brand_terms(wcapi, brands, attrs):
    """Create brand terms in WooCommerce."""
    brand_attr_id = attrs.get('brand')
    if not brand_attr_id:
        return

    print("\nCreating brand terms...")
    for brand in sorted(set(brands)):
        if not brand:
            continue
        data = {'name': brand}
        resp = wcapi.post(f'products/attributes/{brand_attr_id}/terms', data)
        if resp.status_code == 201:
            print(f"  + Created brand: {brand}")
        elif resp.status_code == 400:
            pass  # Already exists


def create_condition_terms(wcapi, attrs):
    """Create condition terms in WooCommerce."""
    condition_attr_id = attrs.get('condition')
    if not condition_attr_id:
        return

    standard_conditions = ['New', 'Like New', 'Excellent', 'Very Good', 'Good', 'Fair']
    print("\nCreating condition terms...")
    for condition in standard_conditions:
        data = {'name': condition}
        resp = wcapi.post(f'products/attributes/{condition_attr_id}/terms', data)
        if resp.status_code == 201:
            print(f"  + Created condition: {condition}")


def ensure_product_category(wcapi, name, slug):
    """カテゴリが存在しなければ作成し、IDを返す"""
    resp = wcapi.get('products/categories', params={'per_page': 100})
    if resp.status_code == 200:
        for cat in resp.json():
            if cat['slug'] == slug:
                return cat['id']

    resp = wcapi.post('products/categories', {'name': name, 'slug': slug})
    if resp.status_code == 201:
        cat_id = resp.json()['id']
        print(f"  Created category: {name} (ID: {cat_id})")
        return cat_id
    return None


def import_products_to_woocommerce(wcapi, products, image_url_map, attrs, category_id=None):
    """商品をWooCommerceにインポート"""
    print(f"\n[5] 商品をWooCommerceにインポート中...")

    imported = 0
    failed = 0
    skipped = 0

    total = len(products)

    for i in range(0, total, CONFIG['batch_size']):
        batch_products = products[i:i + CONFIG['batch_size']]
        batch_num = (i // CONFIG['batch_size']) + 1
        total_batches = (total + CONFIG['batch_size'] - 1) // CONFIG['batch_size']

        print(f"\n  Batch {batch_num}/{total_batches}...")

        batch_data = []
        for product in batch_products:
            sku = product['sku']
            image_urls = image_url_map.get(sku, [])

            # 画像なし商品はスキップ
            if not image_urls:
                print(f"    ~ Skipped (no images): {sku}")
                skipped += 1
                continue

            # カテゴリ設定
            categories = []
            if category_id:
                categories.append({'id': category_id})

            product_data = {
                'name': product['name'],
                'type': 'simple',
                'status': 'publish',
                'sku': sku,
                'regular_price': str(product['price_usd']),
                'description': product['description'],
                'short_description': f"{product['brand']} - {product['condition']}" +
                                     (f" - {product['color']}" if product['color'] else ""),
                'manage_stock': True,
                'stock_quantity': 1,
                'stock_status': 'instock',
                'sold_individually': True,
                'categories': categories,
                'attributes': [],
                'images': [{'src': url} for url in image_urls],
            }

            # Add brand attribute
            if product['brand']:
                product_data['attributes'].append({
                    'id': attrs.get('brand', 0),
                    'name': 'Brand',
                    'visible': True,
                    'options': [product['brand']],
                })

            # Add condition attribute
            if product['condition']:
                product_data['attributes'].append({
                    'id': attrs.get('condition', 0),
                    'name': 'Condition',
                    'visible': True,
                    'options': [product['condition']],
                })

            batch_data.append(product_data)

        # Batch create
        data = {'create': batch_data}
        response = wcapi.post('products/batch', data)

        if response.status_code == 200:
            result = response.json()
            created = result.get('create', [])
            for product in created:
                if 'id' in product and product['id']:
                    imported += 1
                    print(f"    + Created: {product['name'][:50]}... (ID: {product['id']})")
                elif 'error' in product:
                    error = product['error']
                    if error.get('code') == 'product_invalid_sku':
                        skipped += 1
                        print(f"    ~ Skipped (duplicate SKU)")
                    else:
                        failed += 1
                        print(f"    ! Error: {error.get('message', 'Unknown')}")
        else:
            failed += len(batch_data)
            print(f"    ! Batch failed: HTTP {response.status_code}")

        if i + CONFIG['batch_size'] < total:
            time.sleep(CONFIG['delay_between_batches'])

    print(f"\n{'='*50}")
    print(f"インポート完了!")
    print(f"  成功: {imported}")
    print(f"  スキップ: {skipped}")
    print(f"  失敗: {failed}")
    print(f"{'='*50}")

    return imported


def main():
    parser = argparse.ArgumentParser(description='WooCommerce Product Import (統合版)')
    parser.add_argument('--limit', type=int, default=0,
                        help='インポート商品数を制限（0=全件）')
    parser.add_argument('--skip-upload', action='store_true',
                        help='画像アップロードをスキップ（既存のxotrad_image_urls.csvを使用）')
    parser.add_argument('--input', type=str, default='',
                        help='入力CSVファイル')
    args = parser.parse_args()

    print("=" * 50)
    print("Xotrad WooCommerce Import (統合版)")
    print("=" * 50)

    if args.input:
        CONFIG['csv_file'] = args.input

    # Validate
    if not os.path.exists(CONFIG['csv_file']):
        print(f"\n[!] CSV file not found: {CONFIG['csv_file']}")
        sys.exit(1)

    if not os.path.exists(CONFIG['processed_images_dir']):
        print(f"\n[!] Processed images directory not found: {CONFIG['processed_images_dir']}")
        sys.exit(1)

    # 為替レート取得
    print("\n為替レートを取得中...")
    jpy_to_usd_rate = get_usd_jpy_rate()
    usd_to_jpy_rate = 1 / jpy_to_usd_rate
    print(f"  1 USD = {usd_to_jpy_rate:.2f} JPY")

    # Initialize API
    print("\nWooCommerce APIに接続中...")
    wcapi = init_api()

    response = wcapi.get('')
    if response.status_code != 200:
        print(f"[!] API接続失敗: HTTP {response.status_code}")
        sys.exit(1)
    print("  接続成功!")

    # Load products
    products = load_products_from_csv(CONFIG['csv_file'], jpy_to_usd_rate, args.limit)

    if not products:
        print("\n[!] 商品データがありません")
        sys.exit(1)

    # Upload images or load existing URLs
    if args.skip_upload and os.path.exists(CONFIG['output_urls_file']):
        print(f"\n[2-4] 既存の画像URLを読み込み中: {CONFIG['output_urls_file']}")
        image_url_map = {}
        with open(CONFIG['output_urls_file'], 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                sku = row.get('SKU', '')
                urls = [row.get(f'image_url_{i}', '') for i in range(1, 6)]
                urls = [u for u in urls if u]
                image_url_map[sku] = urls
        print(f"  読み込み完了: {len(image_url_map)}件")
    else:
        image_url_map = upload_images_for_products(products, wcapi)
        save_image_urls(image_url_map, CONFIG['output_urls_file'])

    # Create attributes
    print("\n属性を設定中...")
    attrs = create_product_attributes(wcapi)

    # Create brand terms
    brands = [p['brand'] for p in products]
    create_brand_terms(wcapi, brands, attrs)
    create_condition_terms(wcapi, attrs)

    # Ensure Neckties category exists
    print("\nカテゴリを確認中...")
    neckties_cat_id = ensure_product_category(wcapi, 'Neckties', 'neckties')
    if neckties_cat_id:
        print(f"  Neckties category ID: {neckties_cat_id}")

    # Import products
    imported = import_products_to_woocommerce(wcapi, products, image_url_map, attrs, category_id=neckties_cat_id)

    print(f"\n完了! サイトを確認してください:")
    print(f"  {CONFIG['url']}/shop")
    print(f"\n画像URLマッピング: {CONFIG['output_urls_file']}")


if __name__ == '__main__':
    main()
