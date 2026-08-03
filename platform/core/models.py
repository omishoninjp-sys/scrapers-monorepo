"""共用資料結構。各品牌 parse_detail() 一律回傳 Product。"""
from dataclasses import dataclass, field
from typing import Optional

from .config import FEE_TIERS, MIN_FEE_PER_ITEM


@dataclass
class Variant:
    """
    商品規格。伴手禮多半用不到（一件商品一個規格），咖啡與服飾才需要：
    100g/200g、S/M/L、口味選擇都是 variant。

    option_values 對應 Shopify 的 option1/2/3，順序必須與 Product.options 一致。
    image_index 指向 Product.images 的位置 —— 不用圖片網址對應，因為 Shopify
    上傳後會換成自家 CDN 網址，拿原始 src 回頭比對一定對不上。
    """
    sku: str
    price: int = 0                      # 日圓成本（稅込）
    title: str = ""                     # 來源的規格名稱，僅供記錄
    in_stock: bool = True
    option_values: list = field(default_factory=list)
    image_index: Optional[int] = None

    @property
    def selling_price(self) -> int:
        return calculate_selling_price(self.price)


@dataclass
class Product:
    sku: str
    url: str
    title: str = ""
    price: int = 0                      # 日圓成本（稅込）
    in_stock: bool = True
    description: str = ""
    images: list = field(default_factory=list)
    weight: float = 0.0                 # kg，計費重（見 billable_weight）
    actual_weight: float = 0.0          # kg，實重
    volume_weight: float = 0.0          # kg，材積重
    raw: dict = field(default_factory=dict)   # 品牌自訂欄位放這裡
    # 多規格商品才填。空 list = 單一規格，行為與加入 variant 層之前完全相同。
    variants: list = field(default_factory=list)      # [Variant]
    options: list = field(default_factory=list)       # [{"name":..., "values":[...]}]

    @property
    def selling_price(self) -> int:
        return calculate_selling_price(self.price)

    @property
    def sellable_variants(self) -> list:
        return [v for v in self.variants if v.in_stock]

    @property
    def all_skus(self) -> list:
        """
        這件商品在 Shopify 上會佔用的所有 SKU。

        多規格商品的商品層 SKU 取自「第一個有貨的規格」，而那個規格可能賣完，
        於是下一輪的商品層 SKU 就換成另一個 —— 只比對單一 SKU 會把既有商品
        誤判成新商品而重覆上架。改成任一 SKU 命中就算已存在。
        """
        return [self.sku] + [v.sku for v in self.sellable_variants]

    def is_sellable(self, min_price: int) -> Optional[str]:
        """可上架則回傳 None，否則回傳跳過原因"""
        if not self.title:
            return "no_title"
        if not self.in_stock:
            return "out_of_stock"
        # ¥0 是資料缺失或季節限定尚未開賣，不是「便宜商品」。
        # min_price=0（不設門檻）時 price<min_price 永遠不成立，必須獨立擋。
        if self.price <= 0:
            return "no_price"
        if min_price and self.price < min_price:
            return "below_min_price"
        return None


def billable_weight(actual, volume):
    """
    計費重。政策：依實重計費，材積重在實重三倍以內不加收材積費。
    超過三倍才改用材積重（避免超大包裝的極端案例）。
    """
    actual = actual or 0
    volume = volume or 0
    if not actual:
        return round(volume, 2)
    if not volume or volume <= actual * 3:
        return round(actual, 2)
    return round(volume, 2)


def calculate_selling_price(cost) -> int:
    """逐件計費：依成本區間套倍率，服務費不足 ¥300 以 ¥300 計"""
    if not cost or cost <= 0:
        return 0
    for ceiling, rate in FEE_TIERS:
        if ceiling is None or cost <= ceiling:
            break
    fee = round(cost * (rate - 1))
    if fee < MIN_FEE_PER_ITEM:
        fee = MIN_FEE_PER_ITEM
    return round(cost + fee)
