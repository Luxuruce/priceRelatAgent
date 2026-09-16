"""标题归一化与品牌/品类提取。

模糊匹配直接拿原始标题去比会很差：各平台标题充斥促销词、店铺前缀、
规格重复描述。先洗成「品牌 + 核心品名」再比，召回质量差别很大。
"""

from __future__ import annotations

import re

from .spec_parser import normalize_text

# 促销与平台噪声词，匹配前一律剔除
_STOPWORDS = [
    "自营", "旗舰店", "官方", "正品", "包邮", "顺丰", "当日达", "次日达",
    "新品", "热卖", "爆款", "限时", "特价", "促销", "秒杀", "直降", "券后",
    "到手价", "整箱", "批发", "囤货", "家庭装", "量贩装", "临期",
    "进口", "国产", "现货", "预售", "赠品", "试用装",
]
_RE_STOPWORDS = re.compile("|".join(map(re.escape, _STOPWORDS)))

# 括号内容通常是补充说明，对匹配是噪声
_RE_BRACKET = re.compile(r"[\[\(【〔《][^\]\)】〕》]*[\]\)】〕》]")
# 促销短语
_RE_PROMO = re.compile(r"(第二件\d*折|买\d+送\d+|满\d+减\d+|\d+折起|\d+折|立减\d+)")
# 规格片段：归一化标题时去掉，规格由 spec_parser 单独负责
_RE_SPEC_FRAG = re.compile(
    r"\d+(?:\.\d+)?\s*(?:mg|毫克|kg|千克|公斤|g|克|斤|两|磅|lb|oz|盎司|ml|毫升|cl|l|升)"
    r"(?:\s*\*\s*\d+)?",
    re.IGNORECASE,
)
_RE_COUNT_FRAG = re.compile(
    r"\d+\s*(?:连包|件装|只装|个装|片装|袋装|盒装|包装|瓶|罐|盒|袋|包|支|个|只|片|条|卷|组|入|杯|块|枚|提|听|桶|件)"
)
# 非中英数字符
_RE_PUNCT = re.compile(r"[^\w一-鿿]+")
# 剥离规格后残留的孤立量词（"550ml*24瓶" 去掉数字部分只剩 "瓶"），前后不接其他字词时清掉
_RE_DANGLING_UNIT = re.compile(
    r"(?<![\w\u4e00-\u9fff])(?:连包|件装|只装|个装|片装|袋装|盒装|包装|瓶|罐|盒|袋|包|支|只|片|条|卷|组|入|杯|块|枚|提|听|桶|件)(?![\w\u4e00-\u9fff])"
)

# 常见商超品牌词典。命中品牌能大幅提升 L1 规则匹配命中率。
# 实际落地时应从商品主数据导出，这里给一份起步词典。
BRANDS = [
    "农夫山泉", "怡宝", "娃哈哈", "百岁山", "康师傅", "统一", "今麦郎",
    "可口可乐", "百事可乐", "雪碧", "芬达", "美年达", "元气森林", "东鹏特饮", "红牛",
    "伊利", "蒙牛", "光明", "特仑苏", "金典", "安慕希", "纯甄", "君乐宝", "三元",
    "金龙鱼", "福临门", "鲁花", "多力", "胡姬花",
    "海天", "李锦记", "厨邦", "太太乐", "老干妈",
    "三只松鼠", "良品铺子", "百草味", "洽洽", "乐事", "奥利奥", "达利园", "好丽友",
    "雀巢", "星巴克", "麦斯威尔", "立顿",
    "维达", "清风", "心相印", "洁柔", "得宝",
    "蓝月亮", "立白", "雕牌", "汰渍", "碧浪", "奥妙",
    "舒肤佳", "力士", "海飞丝", "潘婷", "飘柔", "清扬", "沙宣",
    "云南白药", "高露洁", "佳洁士", "黑人",
    "五常", "金沙河", "香满园",
    "双汇", "金锣", "雨润", "得利斯",
    "安佳", "总统", "president", "卡夫", "费列罗", "德芙", "士力架",
]
_RE_BRAND = re.compile("|".join(map(re.escape, sorted(BRANDS, key=len, reverse=True))), re.IGNORECASE)

# 品类关键词：用于粗分桶，避免「牛奶」和「牛肉」互相召回
CATEGORY_HINTS = {
    "饮用水": ["饮用水", "天然水", "矿泉水", "纯净水"],
    "碳酸饮料": ["可乐", "汽水", "雪碧", "芬达", "苏打"],
    "牛奶": ["牛奶", "纯牛奶", "酸奶", "乳饮料", "早餐奶"],
    "食用油": ["食用油", "调和油", "花生油", "玉米油", "菜籽油", "橄榄油", "葵花籽油"],
    "大米": ["大米", "稻花香", "珍珠米", "丝苗米", "香米"],
    "面条": ["红烧牛肉面", "香辣牛肉面", "牛肉面", "面条", "挂面", "方便面", "拉面", "泡面", "米线"],
    "调味品": ["酱油", "生抽", "老抽", "米醋", "陈醋", "蚝油", "料酒", "味精", "鸡精"],
    "零食": ["坚果", "薯片", "饼干", "巧克力", "糖果", "果冻", "辣条", "果干"],
    "纸品": ["抽纸", "卷纸", "纸巾", "手帕纸", "湿巾"],
    "洗涤": ["洗衣液", "洗洁精", "洗衣粉", "柔顺剂", "消毒液"],
    "个护": ["洗发水", "沐浴露", "牙膏", "牙刷", "香皂", "洗手液"],
    "肉类": ["猪肉", "牛肉", "鸡肉", "排骨", "香肠", "火腿"],
    "咖啡茶": ["咖啡", "红茶", "绿茶", "茶包", "速溶"],
}

# 预先摊平成 (关键词, 品类) 并按长度降序，保证「牛肉面」优先于「牛肉」命中
_FLAT_HINTS: list[tuple[str, str]] = sorted(
    ((hint, cat) for cat, hints in CATEGORY_HINTS.items() for hint in hints),
    key=lambda pair: len(pair[0]),
    reverse=True,
)


def clean_title(title: str) -> str:
    """把标题洗成适合模糊匹配的形态：去噪声、去规格、去标点。"""
    s = normalize_text(title)
    s = _RE_BRACKET.sub(" ", s)
    s = _RE_PROMO.sub(" ", s)
    s = _RE_STOPWORDS.sub(" ", s)
    s = _RE_SPEC_FRAG.sub(" ", s)
    s = _RE_COUNT_FRAG.sub(" ", s)
    s = _RE_PUNCT.sub(" ", s)
    # 规格片段被剥离后常留下孤立量词（"550ml*24瓶" 去掉数字部分只剩 "瓶"），一并清掉
    s = _RE_DANGLING_UNIT.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def extract_brand(title: str, declared: str = "") -> str:
    """提取品牌。平台已声明品牌则优先采信，否则从标题匹配词典。"""
    if declared and declared.strip():
        return declared.strip()
    if m := _RE_BRAND.search(normalize_text(title)):
        return m.group(0)
    return ""


def extract_category(title: str, declared: str = "") -> str:
    """提取品类。用于匹配前分桶，缩小候选集并避免跨品类误配。"""
    if declared and declared.strip():
        return declared.strip()
    s = normalize_text(title).lower()
    for hint, cat in _FLAT_HINTS:
        if hint in s:
            return cat
    return ""


def char_bigrams(text: str) -> set[str]:
    """中文按字分词效果差，用字符二元组做集合相似度更稳。"""
    s = text.replace(" ", "")
    if len(s) < 2:
        return {s} if s else set()
    return {s[i : i + 2] for i in range(len(s) - 1)}


# ------------------------------------------------------------------
# 口味 / 变体识别
#
# 商超比价最常见的误配来源：品牌、品类、规格三者全同，但口味或属性不同。
# 「康师傅红烧牛肉面 103g*5」和「康师傅香辣牛肉面 103g*5」在这三个维度上
# 完全一致，仅靠规则无法区分 —— 必须显式识别变体词。
#
# 同组内的词互斥：两个商品各自命中同组的不同词，即判定为不同商品。
# 跨组不冲突：「有机」和「原味」可以共存。
# ------------------------------------------------------------------
VARIANT_GROUPS: dict[str, list[str]] = {
    "口味": [
        "红烧", "香辣", "麻辣", "老坛酸菜", "酸菜", "番茄", "咖喱", "香菇",
        "排骨", "海鲜", "五香", "孜然", "烧烤", "原味", "清淡",
    ],
    "甜味": [
        "巧克力", "草莓", "香草", "抹茶", "芒果", "蓝莓", "椰子", "焦糖", "蜂蜜",
    ],
    "脂肪": ["全脂", "低脂", "脱脂", "半脱脂"],
    "糖分": ["无糖", "零糖", "低糖", "含糖", "代糖"],
    "咖啡因": ["低因", "脱因", "无咖啡因"],
    "工艺": ["有机", "非转基因", "鲜榨", "浓缩", "冷萃", "现磨", "速溶"],
    "形态": ["袋装", "罐装", "瓶装", "盒装", "桶装", "散装"],
    "色系": ["纯白", "薰衣草", "芦荟", "柠檬", "海洋", "樱花"],
}

# 摊平成 关键词 -> 组名；长词优先，避免「酸菜」抢在「老坛酸菜」之前
_VARIANT_LOOKUP: list[tuple[str, str]] = sorted(
    ((word, group) for group, words in VARIANT_GROUPS.items() for word in words),
    key=lambda pair: len(pair[0]),
    reverse=True,
)


def extract_variants(title: str) -> dict[str, str]:
    """提取商品的变体特征，返回 {组名: 命中词}。

    每组只取第一个（最长）命中词，避免「原味」和「香辣」同时出现在
    促销文案里造成误判。
    """
    s = normalize_text(title)
    found: dict[str, str] = {}
    for word, group in _VARIANT_LOOKUP:
        if group not in found and word in s:
            found[group] = word
    return found


def variant_conflict(a: str, b: str) -> str:
    """判断两个标题是否存在变体冲突。

    Returns:
        冲突描述；无冲突返回空串。
        仅当两侧在**同一组**内都识别出了词、且词不同时才算冲突 ——
        一侧没写口味不代表口味不同，不能据此否决。
    """
    va, vb = extract_variants(a), extract_variants(b)
    for group in va.keys() & vb.keys():
        # 形态（袋装/罐装）不影响是否同款商品，仅作参考不构成冲突
        if group == "形态":
            continue
        if va[group] != vb[group]:
            return f"{group}不同（{va[group]} vs {vb[group]}）"
    return ""


# ------------------------------------------------------------------
# 子品牌 / 产品线识别
#
# 「蒙牛纯牛奶」和「蒙牛特仑苏纯牛奶」品牌、品类、规格全同，但特仑苏是
# 独立产品线、价格带完全不同，拿来比价没有意义。
#
# 与口味冲突的区别在于**不对称性**：一方标了特仑苏、另一方没标，本身
# 就说明它们不是同一款。但标题也可能只是省略了系列名，所以这里不做硬
# 排除，只降级到灰区交人工或 AI 判定 —— 漏配可以补，错配会直接改错价。
# ------------------------------------------------------------------
SUB_BRANDS = [
    # 乳制品
    "特仑苏", "金典", "安慕希", "纯甄", "优酸乳", "每益添", "QQ星", "臻浓", "未来星",
    # 粮油调味
    "黄金比例", "金标", "味极鲜", "一级压榨", "稻花香", "长粒香",
    # 饮料
    "无糖茶π", "茶π", "尖叫", "维他命水", "东方树叶",
    # 休闲食品
    "无限", "每日坚果", "夹心", "威化",
    # 咖啡
    "醇品", "金牌", "丝滑拿铁",
    # 家清个护
    "蓝色经典", "超韧", "深层洁净", "亮白增艳", "自然阳光",
    # 肉制品
    "王中王", "润口香甜",
]
_RE_SUB_BRAND = re.compile(
    "|".join(map(re.escape, sorted(SUB_BRANDS, key=len, reverse=True))), re.IGNORECASE
)


def extract_sub_brand(title: str) -> str:
    """提取子品牌 / 产品线，未命中返回空串。"""
    m = _RE_SUB_BRAND.search(normalize_text(title))
    return m.group(0) if m else ""


def sub_brand_mismatch(a: str, b: str) -> str:
    """判断两个标题的产品线是否不一致（含一方未标注的情况）。

    Returns:
        差异描述；一致时返回空串。
    """
    sa, sb = extract_sub_brand(a), extract_sub_brand(b)
    if sa == sb:
        return ""
    if sa and sb:
        return f"产品线不同（{sa} vs {sb}）"
    return f"仅一方标注产品线（{sa or sb}）"
