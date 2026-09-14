"""
Pica API 常量

签名机制与哔咔官方 App 一致（HMAC-SHA256），由旧版 zhenxun_plugin_pica 逆向而来，
经实测当前 (2026-08) 依然有效。
"""

BASE_URL = "https://picaapi.picacomic.com"

# ---- 签名密钥（App 逆向得到）----
NONCE = "b1ab87b4800d4d4590a11701b8551afa"
API_KEY = "C69BAF41DA5ABD1FFEDC6D2FEA56B"
SECRET_KEY = r"~d}$Q7$eIni=V)9\RK/P.RM4;9[7|@/CA}b~OW!3?EV`:<>M7pddUBL5n|0/*Cn"

# ---- 请求头常量（与 App 一致，缺一不可）----
APP_VERSION = "2.2.1.2.3.3"
APP_UUID = "defaultUuid"
APP_PLATFORM = "android"
APP_BUILD_VERSION = "44"
APP_CHANNEL = "2"
USER_AGENT = "okhttp/3.8.1"
ACCEPT_JSON = "application/vnd.picacomic.com.v1+json"

# ---- 排序方式 ----
ORDERS = {
    "ua": "默认",
    "dd": "新到旧",
    "da": "旧到新",
    "ld": "最多爱心",
    "vd": "最多指名",
}

# ---- 排行榜时间范围 ----
RANK_TYPES = {
    "H24": "日榜(24H)",
    "D7": "周榜(7D)",
    "D30": "月榜(30D)",
}

# ---- 全部分区 ----
CATEGORIES = [
    "重口地帶", "Cosplay", "歐美", "禁書目錄", "WEBTOON", "東方", "Fate",
    "SAO 刀劍神域", "Love Live", "艦隊收藏", "非人類", "強暴", "NTR", "人妻",
    "足の恋", "性轉換", "SM", "妹妹系", "姐姐系", "扶他樂園", "後宮閃光",
    "偽娘哲學", "耽美花園", "百合花園", "純愛", "生肉", "英語 ENG", "CG雜圖",
    "碧藍幻想", "圓神領域", "短篇", "長篇", "全彩", "嗶咔漢化",
]

# ---- 认证错误码（API 返回的 error 字段是字符串，统一按字符串匹配）----
AUTH_ERROR_CODES = {"401", "1005", "1008"}
