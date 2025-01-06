import os.path
import re

import log
from app.downloader import Downloader
from app.helper import DbHelper, ProgressHelper
from app.indexer import Indexer
from app.media import Media, DouBan
from app.media.meta import MetaInfo
from app.message import Message
from app.searcher import Searcher
from app.sites import Sites
from app.subscribe import Subscribe
from app.utils import StringUtils, Torrent
from app.utils.types import SearchType, IndexerType
from config import Config
from web.backend.web_utils import WebUtils

SEARCH_MEDIA_CACHE = {}
SEARCH_MEDIA_TYPE = {}


def search_medias_for_web(content, ident_flag=True, filters=None, tmdbid=None, media_type=None):
    """
    WEB资源搜索
    :param content: 关键字文本，可以包括 类型、标题、季、集、年份等信息，使用 空格分隔，也支持种子的命名格式
    :param ident_flag: 是否进行媒体信息识别
    :param filters: 其它过滤条件
    :param tmdbid: TMDBID或DB:豆瓣ID
    :param media_type: 媒体类型，配合tmdbid传入
    :return: 错误码，错误原因，成功时直接插入数据库
    """
    mtype, key_word, season_num, episode_num, year, content = StringUtils.get_keyword_from_string(content)
    if not key_word:
        log.info("【Web】%s 检索关键字有误！" % content)
        return -1, "%s 未识别到搜索关键字！" % content
    # 类型
    if media_type:
        mtype = media_type
    # 开始进度
    search_process = ProgressHelper()
    search_process.start('search')
    # 识别媒体
    media_info = None
    if ident_flag:

        # 有TMDBID或豆瓣ID
        if tmdbid:
            media_info = WebUtils.get_mediainfo_from_id(mtype=mtype, mediaid=tmdbid)
        else:
            # 按输入名称查
            media_info = Media().get_media_info(mtype=media_type or mtype,
                                                title=content)

        # 整合集
        if media_info:
            if season_num:
                media_info.begin_season = int(season_num)
            if episode_num:
                media_info.begin_episode = int(episode_num)

        if media_info and media_info.tmdb_info:
            # 查询到TMDB信息
            log.info(f"【Web】从TMDB中匹配到{media_info.type.value}：{media_info.get_title_string()}")
            # 查找的季
            if media_info.begin_season is None:
                search_season = None
            else:
                search_season = media_info.get_season_list()
            # 查找的集
            search_episode = media_info.get_episode_list()
            if search_episode and not search_season:
                search_season = [1]
            # 中文名
            if media_info.cn_name:
                search_cn_name = media_info.cn_name
            else:
                search_cn_name = media_info.title
            # 英文名
            search_en_name = None
            if media_info.en_name:
                search_en_name = media_info.en_name
            else:
                if media_info.original_language == "en":
                    search_en_name = media_info.original_title
                else:
                    en_title = Media().get_tmdb_en_title(media_info)
                    if en_title:
                        search_en_name = en_title
            # 两次搜索名称
            second_search_name = None
            if Config().get_config("laboratory").get("search_en_title"):
                if search_en_name:
                    first_search_name = search_en_name
                    second_search_name = search_cn_name
                else:
                    first_search_name = search_cn_name
            else:
                first_search_name = search_cn_name
                if search_en_name:
                    second_search_name = search_en_name

            filter_args = {"season": search_season,
                           "episode": search_episode,
                           "year": media_info.year,
                           "type": media_info.type}
        else:
            # 查询不到数据，使用快速搜索
            log.info(f"【Web】{content} 未从TMDB匹配到媒体信息，将使用快速搜索...")
            ident_flag = False
            media_info = None
            first_search_name = key_word
            second_search_name = None
            filter_args = {
                "season": season_num,
                "episode": episode_num,
                "year": year
            }
    # 快速搜索
    else:
        first_search_name = key_word
        second_search_name = None
        filter_args = {
            "season": season_num,
            "episode": episode_num,
            "year": year
        }
    # 整合高级查询条件
    if filters:
        filter_args.update(filters)
    # 开始检索
    log.info("【Web】开始检索 %s ..." % content)
    media_list = Searcher().search_medias(key_word=first_search_name,
                                          filter_args=filter_args,
                                          match_media=media_info,
                                          in_from=SearchType.WEB)
    # 使用第二名称重新搜索
    if ident_flag \
            and len(media_list) == 0 \
            and second_search_name \
            and second_search_name != first_search_name:
        search_process.start('search')
        search_process.update(ptype='search',
                              text="%s 未检索到资源,尝试通过 %s 重新检索 ..." % (first_search_name, second_search_name))
        log.info("【Searcher】%s 未检索到资源,尝试通过 %s 重新检索 ..." % (first_search_name, second_search_name))
        media_list = Searcher().search_medias(key_word=second_search_name,
                                              filter_args=filter_args,
                                              match_media=media_info,
                                              in_from=SearchType.WEB)
    # 结束进度
    search_process.end('search')
    if len(media_list) == 0:
        log.info("【Web】%s 未检索到任何资源" % content)
        return 1, "%s 未检索到任何资源" % content
    else:
        log.info("【Web】共检索到 %s 个有效资源" % len(media_list))
        # 清空缓存结果
        dbhepler = DbHelper()
        dbhepler.delete_all_search_torrents()
        # 插入数据库
        media_list = sorted(media_list, key=lambda x: "%s%s%s" % (str(x.res_order).rjust(3, '0'),
                                                                  str(x.site_order).rjust(3, '0'),
                                                                  str(x.seeders).rjust(10, '0')), reverse=True)
        dbhepler.insert_search_results(media_items=media_list,
                                       ident_flag=ident_flag,
                                       title=content)
        return 0, ""


def search_media_by_message(input_str, in_from: SearchType, user_id, user_name=None):
    """
    输入字符串，解析要求并进行资源检索
    :param input_str: 输入字符串，可以包括标题、年份、季、集的信息，使用空格隔开
    :param in_from: 搜索下载的请求来源
    :param user_id: 需要发送消息的，传入该参数，则只给对应用户发送交互消息
    :param user_name: 用户名称
    :return: 请求的资源是否全部下载完整、请求的文本对应识别出来的媒体信息、请求的资源如果是剧集，则返回下载后仍然缺失的季集信息
    """
    global SEARCH_MEDIA_TYPE
    global SEARCH_MEDIA_CACHE

    if not input_str:
        log.info("【Searcher】检索关键字有误！")
        return
    # 如果是数字，表示选择项
    if input_str.isdigit() and int(input_str) < 10:
        # 获取之前保存的可选项
        choose = int(input_str) - 1
        if not SEARCH_MEDIA_CACHE.get(user_id) or \
                choose < 0 or choose >= len(SEARCH_MEDIA_CACHE.get(user_id)):
            Message().send_channel_msg(channel=in_from,
                                       title="输入错误",
                                       user_id=user_id)
            log.warn("【Searcher】错误的输入值：%s" % input_str)
            return
        media_info = SEARCH_MEDIA_CACHE[user_id][choose]
        if not SEARCH_MEDIA_TYPE.get(user_id) \
                or SEARCH_MEDIA_TYPE.get(user_id) == "SEARCH":
            # 如果是豆瓣数据，需要重新查询TMDB的数据
            if media_info.douban_id:
                _title = media_info.get_title_string()
                # 先从网页抓取（含TMDBID）
                doubaninfo = DouBan().get_media_detail_from_web(media_info.douban_id)
                if doubaninfo and doubaninfo.get("imdbid"):
                    tmdbid = Media().get_tmdbid_by_imdbid(doubaninfo.get("imdbid"))
                    if tmdbid:
                        # 按IMDBID查询TMDB
                        media_info.set_tmdb_info(Media().get_tmdb_info(mtype=media_info.type, tmdbid=tmdbid))
                        media_info.imdb_id = doubaninfo.get("imdbid")
                else:
                    search_episode = media_info.begin_episode
                    media_info = Media().get_media_info(title="%s %s" % (media_info.title, media_info.year),
                                                        mtype=media_info.type,
                                                        strict=True)
                    media_info.begin_episode = search_episode
                if not media_info or not media_info.tmdb_info:
                    Message().send_channel_msg(channel=in_from,
                                               title="【%s】从TMDB查询不到媒体信息" % _title,
                                               user_id=user_id)
                    return
            # 搜索
            __search_media(in_from=in_from,
                           media_info=media_info,
                           user_id=user_id,
                           user_name=user_name)
        elif not SEARCH_MEDIA_TYPE.get(user_id) or SEARCH_MEDIA_TYPE.get(user_id) == "DOWNLOAD":
            # 添加下载
            ret, msg = Downloader().download(media_info=media_info)
            if ret:
                Message().send_channel_msg(channel=in_from,
                                           title="【%s】下载成功" % media_info.org_string,
                                           user_id=user_id)
            else:
                Message().send_channel_msg(channel=in_from,
                                           title="【%s】下载失败：%s" % (media_info.org_string, msg),
                                           user_id=user_id)
        else:
            # 订阅
            __rss_media(in_from=in_from,
                        media_info=media_info,
                        user_id=user_id,
                        user_name=user_name)
    # 接收到文本，开始查询可能的媒体信息供选择
    else:
        if input_str.startswith("订阅"):
            SEARCH_MEDIA_TYPE[user_id] = "SUBSCRIBE"
            input_str = re.sub(r"订阅[:：\s]*", "", input_str)
        elif input_str.startswith("http") or input_str.startswith("magnet:"):
            SEARCH_MEDIA_TYPE[user_id] = "DOWNLOAD"
        else:
            input_str = re.sub(r"(搜索|下载)[:：\s]*", "", input_str)
            SEARCH_MEDIA_TYPE[user_id] = "SEARCH"

        # 下载链接
        if SEARCH_MEDIA_TYPE[user_id] == "DOWNLOAD":
            if input_str.startswith("http"):
                # 检查是不是有这个站点
                site_info = Sites().get_sites(siteurl=input_str)
                # 偿试下载种子文件
                filepath, content, retmsg = Torrent().save_torrent_file(
                    url=input_str,
                    cookie=site_info.get("cookie"),
                    ua=site_info.get("ua"),
                    proxy=site_info.get("proxy")
                )
                # 下载种子出错
                if not content and retmsg:
                    Message().send_channel_msg(channel=in_from,
                                               title=retmsg,
                                               user_id=user_id)
                    return
                if isinstance(content, str):
                    # 磁力链
                    title = Torrent().get_magnet_title(content)
                    if title:
                        meta_info = Media().get_media_info(title=title)
                    else:
                        meta_info = MetaInfo(title="磁力链接")
                        meta_info.org_string = content
                    meta_info.set_torrent_info(
                        enclosure=content,
                        download_volume_factor=0,
                        upload_volume_factor=1
                    )
                else:
                    # 识别文件名
                    filename = os.path.basename(filepath)
                    # 识别
                    meta_info = Media().get_media_info(title=filename)
                    meta_info.set_torrent_info(
                        enclosure=input_str
                    )
            else:
                # 磁力链
                filepath = None
                title = Torrent().get_magnet_title(input_str)
                if title:
                    meta_info = Media().get_media_info(title=title)
                else:
                    meta_info = MetaInfo(title="磁力链接")
                    meta_info.org_string = input_str
                meta_info.set_torrent_info(
                    enclosure=input_str,
                    download_volume_factor=0,
                    upload_volume_factor=1
                )
            # 开始下载
            meta_info.user_name = user_name
            state, retmsg = Downloader().download(media_info=meta_info,
                                                  torrent_file=filepath)
            if state:
                Message().send_download_message(in_from=in_from,
                                                can_item=meta_info)
            else:
                Message().send_channel_msg(channel=in_from,
                                           title=f"下载失败：{retmsg}",
                                           user_id=user_id)

        # 搜索或订阅
        else:
            # 获取字符串中可能的RSS站点列表
            rss_sites, content = StringUtils.get_idlist_from_string(input_str,
                                                                    [{
                                                                        "id": site.get("name"),
                                                                        "name": site.get("name")
                                                                    } for site in Sites().get_sites(rss=True)])

            # 索引器类型
            indexer_type = Indexer().get_client_type()
            indexers = Indexer().get_indexers()

            # 获取字符串中可能的搜索站点列表
            if indexer_type == IndexerType.BUILTIN:
                content = input_str
                search_sites, _ = StringUtils.get_idlist_from_string(input_str, [{
                    "id": indexer.name,
                    "name": indexer.name
                } for indexer in indexers])
            else:
                search_sites, content = StringUtils.get_idlist_from_string(content, [{
                    "id": indexer.name,
                    "name": indexer.name
                } for indexer in indexers])

            # 获取字符串中可能的下载设置
            download_setting, content = StringUtils.get_idlist_from_string(content, [{
                "id": dl.get("id"),
                "name": dl.get("name")
            } for dl in Downloader().get_download_setting().values()])
            if download_setting:
                download_setting = download_setting[0]
            if not content:
                Message().send_channel_msg(channel=in_from,
                                           title="无法识别内容",
                                           user_id=user_id)
                return
            # 识别媒体信息，列出匹配到的所有媒体
            log.info("【Searcher】正在识别 %s 的媒体信息" % content)
            medias = WebUtils.search_media_infos(keyword=content, include_adult=True)
            if not SEARCH_MEDIA_TYPE.get(user_id) or SEARCH_MEDIA_TYPE.get(user_id) == "SEARCH":
                media_info = MetaInfo(content)
                media_info.title = content
                media_info.keyword = content
                # 由于消息长度限制,取前7条并在最后拼接一个关键字检索共8条
                medias = medias[:7]
                medias.append(media_info)
            # 保存识别信息到临时结果
            SEARCH_MEDIA_CACHE[user_id] = []
            for meta_info in medias[:8]:
                # 合并站点和下载设置信息
                meta_info.rss_sites = rss_sites
                meta_info.search_sites = search_sites
                meta_info.set_download_info(download_setting=download_setting)
                SEARCH_MEDIA_CACHE[user_id].append(meta_info)

            if 1 == len(SEARCH_MEDIA_CACHE[user_id]) and \
                    (not SEARCH_MEDIA_TYPE.get(user_id) or SEARCH_MEDIA_TYPE.get(user_id) == "SEARCH"):
                # 只有一条数据是没有搜索到媒体信息，直接检索资源
                media_info = SEARCH_MEDIA_CACHE[user_id][0]
                __search_media(in_from=in_from,
                                media_info=media_info,
                                user_id=user_id,
                                user_name=user_name)
            else:
                # 发送消息通知选择
                media_type = "搜索" if SEARCH_MEDIA_TYPE.get(user_id) == "SEARCH" else "订阅"
                Message().send_channel_list_msg(channel=in_from,
                                                title="识别到%s条信息，回复序号%s" % (len(SEARCH_MEDIA_CACHE[user_id]), media_type) \
                                                    if SEARCH_MEDIA_CACHE[user_id] else "未能识别到媒体信息",
                                                medias=SEARCH_MEDIA_CACHE[user_id],
                                                user_id=user_id)


def __search_media(in_from, media_info, user_id, user_name=None):
    """
    开始搜索和发送消息
    """
    global SEARCH_MEDIA_TYPE
    global SEARCH_MEDIA_CACHE

    # adult配置关键字搜索
    if media_info.adult:
        if not media_info.keyword:
            id_str = __get_id(media_info.title)
            if id_str:
                media_info.keyword = id_str
            else:
                media_info.set_tmdb_info(Media().get_tmdb_info(mtype=media_info.type, tmdbid=media_info.tmdb_id))
            if not media_info.keyword:
                media_info.keyword = media_info.title
        media_info.title = media_info.keyword
    # 检查是否存在，电视剧返回不存在的集清单
    exist_flag, no_exists, messages = Downloader().check_exists_medias(meta_info=media_info)
    if messages:
        Message().send_channel_msg(channel=in_from,
                                   title="\n".join(messages),
                                   user_id=user_id)
    # 已经存在
    if exist_flag:
        return
    # 客户端查询条件
    client_filter_name = Config().get_config('app').get('client_filter_name', "")
    groupid = DbHelper().get_filter_groupid_by_name(client_filter_name)
    filter_args = {"rule": groupid} if groupid else {}

    if media_info.keyword:
        _, key_word, season_num, episode_num, year, content = StringUtils.get_keyword_from_string(media_info.keyword)
        if not key_word:
            Message().send_channel_msg(channel=in_from,
                                       title="【%s】中获取检索关键字失败" % content,
                                       user_id=user_id) 
            return
        filter = {
            "season": season_num,
            "episode": episode_num,
            "year": year
        }
        # 添加客户端查询条件
        filter_args.update(filter)
    # 开始检索
    Message().send_channel_msg(channel=in_from,
                               title="正在检索【%s】" % media_info.title,
                               user_id=user_id)
    medias, search_result, no_exists, search_count, download_count = Searcher().search_one_media(media_info=media_info,
                                                                                         in_from=in_from,
                                                                                         no_exists=no_exists,
                                                                                         sites=media_info.search_sites,
                                                                                         filters= filter_args,
                                                                                         user_name=user_name)
    # 没有搜索到数据
    if not search_count:
        Message().send_channel_msg(channel=in_from,
                                   title="【%s】未检索到资源" % media_info.title,
                                   user_id=user_id)
    else:
        # 搜索到了但是没开自动下载或择优下载失败
        if download_count is None and medias:
            # 保存识别信息到临时结果中，由于消息长度限制只取前8条
            SEARCH_MEDIA_CACHE[user_id] = medias[:8]
            SEARCH_MEDIA_TYPE[user_id] = "DOWNLOAD"
            Message().send_channel_list_msg(channel=in_from,
                                            title="检索到%s个资源，回复序号下载" % len(SEARCH_MEDIA_CACHE[user_id]),
                                            medias=SEARCH_MEDIA_CACHE[user_id],
                                            user_id=user_id)
            return
        else:
            # 搜索到了但是没下载到数据
            if download_count == 0:
                Message().send_channel_msg(channel=in_from,
                                           title="【%s】检索到%s个资源，但未下载" % (media_info.title, search_count),
                                           user_id=user_id)
    # 没有下载完成，且打开了自动添加订阅
    if not search_result and not media_info.keyword and Config().get_config('pt').get('search_no_result_rss'):
        # 添加订阅
        __rss_media(in_from=in_from,
                    media_info=media_info,
                    user_id=user_id,
                    state='R',
                    user_name=user_name)


def __rss_media(in_from, media_info, user_id=None, state='D', user_name=None):
    """
    开始添加订阅和发送消息
    """
    title = media_info.title
    # 添加订阅
    if media_info.douban_id:
        code, msg, media_info = Subscribe().add_rss_subscribe(mtype=media_info.type,
                                                              name=media_info.title,
                                                              year=media_info.year,
                                                              season=media_info.begin_season,
                                                              mediaid=f"DB:{media_info.douban_id}",
                                                              state=state,
                                                              rss_sites=media_info.rss_sites,
                                                              search_sites=media_info.search_sites)
    else:
        code, msg, media_info = Subscribe().add_rss_subscribe(mtype=media_info.type,
                                                              name=media_info.title,
                                                              year=media_info.year,
                                                              season=media_info.begin_season,
                                                              mediaid=media_info.tmdb_id,
                                                              state=state,
                                                              rss_sites=media_info.rss_sites,
                                                              search_sites=media_info.search_sites)
    if code == 0:
        log.info("【Web】%s %s 已添加订阅" % (media_info.type.value, media_info.get_title_string()))
        if in_from in Message().get_search_types():
            media_info.user_name = user_name
            Message().send_rss_success_message(in_from=in_from,
                                               media_info=media_info)
    else:
        log.info("【Web】%s 添加订阅失败：%s" % (title, msg))
        if in_from in Message().get_search_types():
            Message().send_channel_msg(channel=in_from,
                                       title="【%s】添加订阅失败：%s" % (title, msg),
                                       user_id=user_id)
            
def __get_id(title_str: str) -> str:
    """从给定的标题中提取番号（DVD ID）"""
    ignored_id_pattern = Config().get_config('app').get('ignored_id_pattern', [])
    ignore_pattern = re.compile('|'.join(ignored_id_pattern))
    norm = ignore_pattern.sub('', title_str).upper()
    if 'FC2' in norm:
        # 根据FC2 Club的影片数据，FC2编号为5-7个数字
        match = re.search(r'FC2[^A-Z\d]{0,5}(PPV[^A-Z\d]{0,5})?(\d{5,7})', norm, re.I)
        if match:
            return 'FC2-' + match.group(2)
    elif 'HEYDOUGA' in norm:
        match = re.search(r'(HEYDOUGA)[-_]*(\d{4})[-_]0?(\d{3,5})', norm, re.I)
        if match:
            return '-'.join(match.groups())
    elif 'GETCHU' in norm:
        match = re.search(r'GETCHU[-_]*(\d+)', norm, re.I)
        if match:
            return 'GETCHU-' + match.group(1)
    elif 'GYUTTO' in norm:
        match = re.search(r'GYUTTO-(\d+)', norm, re.I)
        if match:
            return 'GYUTTO-' + match.group(1)
    elif '259LUXU' in norm: # special case having form of '259luxu'
        match = re.search(r'259LUXU-(\d+)', norm, re.I)
        if match:
            return '259LUXU-' + match.group(1)

    else:
        # 先尝试移除可疑域名进行匹配，如果匹配不到再使用原始文件名进行匹配
        no_domain = re.sub(r'\w{3,10}\.(COM|NET|APP|XYZ)', '', norm, flags=re.I)
        if no_domain != norm:
            avid = __get_id(no_domain)
            if avid:
                return avid
        # 匹配缩写成hey的heydouga影片。由于番号分三部分，要先于后面分两部分的进行匹配
        match = re.search(r'(?:HEY)[-_]*(\d{4})[-_]0?(\d{3,5})', norm, re.I)
        if match:
            return 'heydouga-' + '-'.join(match.groups())
        # 匹配片商 MUGEN 的奇怪番号。由于MK3D2DBD的模式，要放在普通番号模式之前进行匹配
        match = re.search(r'(MKB?D)[-_]*(S\d{2,3})|(MK3D2DBD|S2M|S2MBD)[-_]*(\d{2,3})', norm, re.I)
        if match:
            if match.group(1) is not None:
                avid = match.group(1) + '-' + match.group(2)
            else:
                avid = match.group(3) + '-' + match.group(4)
            return avid
        # 匹配IBW这样带有后缀z的番号
        match = re.search(r'(IBW)[-_](\d{2,5}z)', norm, re.I)
        if match:
            return match.group(1) + '-' + match.group(2)
        # 普通番号，优先尝试匹配带分隔符的（如ABC-123）
        match = re.search(r'([A-Z]{2,10})[-_](\d{2,5})', norm, re.I)
        if match:
            return match.group(1) + '-' + match.group(2)
        # 普通番号，运行到这里时表明无法匹配到带分隔符的番号
        # 先尝试匹配东热的red, sky, ex三个不带-分隔符的系列
        # （这三个系列已停止更新，因此根据其作品编号将数字范围限制得小一些以降低误匹配概率）
        match = re.search(r'(RED[01]\d\d|SKY[0-3]\d\d|EX00[01]\d)', norm, re.I)
        if match:
            return match.group(1)
        # 然后再将影片视作缺失了-分隔符来匹配
        match = re.search(r'([A-Z]{2,})(\d{2,5})', norm, re.I)
        if match:
            return match.group(1) + '-' + match.group(2)
    # 尝试匹配TMA制作的影片（如'T28-557'，他家的番号很乱）
    match = re.search(r'(T[23]8[-_]\d{3})', norm)
    if match:
        return match.group(1)
    # 尝试匹配东热n, k系列
    match = re.search(r'(N\d{4}|K\d{4})', norm, re.I)
    if match:
        return match.group(1)
    # 尝试匹配R18-XXX的番号
    match = re.search(r'R18-?\d{3}', norm, re.I)
    if match:
        return match.group(1)
    # 尝试匹配纯数字番号（无码影片）
    match = re.search(r'(\d{6}[-_]\d{2,3})', norm)
    if match:
        return match.group(1)
    # 如果还是匹配不了，尝试将' '替换为'-'后再试，少部分影片的番号是由' '分隔的
    if ' ' in norm:
        avid1 = __get_id(norm.replace(' ', '-'))
        if avid1:
            return avid1
    # 如果还是匹配不了，尝试将')('替换为'-'后再试，少部分影片的番号是由')('分隔的
    if ')(' in norm:
        avid2 = __get_id(norm.replace(')(', '-'))
        if avid2:
            return avid2
    return ''
