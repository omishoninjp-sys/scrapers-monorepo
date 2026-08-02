"""國際運費表 HTML。12 支 scraper 原本各存一份完全相同的副本，這裡統一。"""

SHIPPING_HTML = '<div style="margin-top:24px;border-top:1px solid #e8eaf0;padding-top:20px;"><h2 style="font-size:16px;font-weight:700;color:#1a1a2e;border-bottom:2px solid #e8eaf0;padding-bottom:8px;margin:0 0 16px;">國際運費（空運・包稅）</h2><p style="margin:0 0 6px;font-size:13px;color:#444;">✓ 含國際運費\u3000✓ 含關稅\u3000✓ 含台灣配送費\u3000✓ 無隱藏費用</p><p style="margin:0 0 12px;font-size:13px;color:#444;">本頁標示價格<strong>僅含商品費用</strong>，運費於商品到倉、確認實際重量後另行請款（二段式收費）。依實重計費，材積重在實重三倍以內不加收材積費。</p><table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:10px;"><tbody><tr style="background:#f0f4ff;"><td style="padding:9px 14px;border:1px solid #dde3f0;">≦ 1.0 kg</td><td style="padding:9px 14px;border:1px solid #dde3f0;font-weight:600;">¥1,000 <span style="color:#888;font-weight:400;">≈ NT$200</span></td></tr><tr style="background:#fff;"><td style="padding:9px 14px;border:1px solid #dde3f0;">1.1 ～ 1.5 kg</td><td style="padding:9px 14px;border:1px solid #dde3f0;font-weight:600;">¥1,500 <span style="color:#888;font-weight:400;">≈ NT$300</span></td></tr><tr style="background:#f0f4ff;"><td style="padding:9px 14px;border:1px solid #dde3f0;">1.6 ～ 2.0 kg</td><td style="padding:9px 14px;border:1px solid #dde3f0;font-weight:600;">¥2,000 <span style="color:#888;font-weight:400;">≈ NT$400</span></td></tr><tr style="background:#fff;"><td style="padding:9px 14px;border:1px solid #dde3f0;">2.1 ～ 2.5 kg</td><td style="padding:9px 14px;border:1px solid #dde3f0;font-weight:600;">¥2,500 <span style="color:#888;font-weight:400;">≈ NT$500</span></td></tr><tr style="background:#f0f4ff;"><td style="padding:9px 14px;border:1px solid #dde3f0;">2.6 ～ 3.0 kg</td><td style="padding:9px 14px;border:1px solid #dde3f0;font-weight:600;">¥3,000 <span style="color:#888;font-weight:400;">≈ NT$600</span></td></tr><tr style="background:#fff;"><td style="padding:9px 14px;border:1px solid #dde3f0;color:#555;">每增加 0.5 kg</td><td style="padding:9px 14px;border:1px solid #dde3f0;color:#555;">+¥500\u3000<span style="color:#888;">+≈ NT$100</span></td></tr></tbody></table><p style="margin:0 0 28px;font-size:12px;color:#999;">NT$ 匯率僅供參考，實際以下單當日匯率為準。起運 1 kg，未滿 1 kg 以 1 kg 計算。</p></div>'


def shipping_fee(kg):
    """包稅空運運費（日圓）。起運 1 kg，未滿 1 kg 以 1 kg 計。"""
    import math
    if not kg or kg <= 0:
        return 0
    if kg <= 1.0:
        return 1000
    if kg <= 3.0:
        return 1000 + math.ceil((kg - 1.0) / 0.5) * 500
    return 3000 + math.ceil((kg - 3.0) / 0.5) * 500
