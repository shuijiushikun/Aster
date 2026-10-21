"""演示数据的精确中文映射：兼容已落库的数据，不修改任意客户提供的文本。"""

_PRODUCT_NAMES = {
    "Wireless Headphones": "无线耳机",
    "Clearance Graphic T-Shirt": "清仓印花 T 恤",
    '55" 4K OLED TV': "55 英寸 4K OLED 电视",
    "Bluetooth Speaker": "蓝牙音箱",
    "Trail Running Shoes": "越野跑鞋",
    "Drip Coffee Maker": "滴滤咖啡机",
    "Leather Designer Handbag": "真皮设计师手袋",
    "Silicone Phone Case": "硅胶手机壳",
    "Final Sale Winter Jacket": "清仓冬季外套",
    "Aluminum Laptop Stand": "铝合金笔记本支架",
    "Gaming Console": "游戏主机",
    "Extra Wireless Controller": "备用无线手柄",
    "Diamond Stud Earrings": "钻石耳钉",
    "Premium Yoga Mat": "高品质瑜伽垫",
    "Smart Watch Series 7": "第七代智能手表",
    "Clearance Watch Band": "清仓表带",
    "Ergonomic Office Chair": "人体工学办公椅",
    "Electric Standing Desk": "电动升降桌",
    "Espresso Machine": "意式咖啡机",
    "Handheld Milk Frother": "手持奶泡器",
    "Noise-Cancelling Headphones": "降噪耳机",
    "Clearance Hoodie": "清仓连帽衫",
    "Ultrabook Laptop": "轻薄笔记本电脑",
    "Desk Lamp": "台灯",
    "Mechanical Keyboard": "机械键盘",
}


def product_name(value: str) -> str:
    return _PRODUCT_NAMES.get(value, value)


ORDER_STATUS = {
    "delivered": "已送达",
    "shipped": "运输中",
    "pending": "待处理",
    "processing": "处理中",
    "cancelled": "已取消",
}
