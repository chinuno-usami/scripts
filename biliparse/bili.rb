#!/usr/bin/env ruby
require 'net/http'
require 'json'
require 'date'
#require 'pp'
#require 'pry'

def url2Href(url)
  return '' if url.nil? || url.empty?
  link = url.start_with?('http://', 'https://') ? url : ('https:' + url)
  '<a href="' + link + '">' + link + '</a>'
end

def s(v)
  v.nil? ? '' : v.to_s
end

def parseItem(item)
  ret = Hash.new()
  ret['link'] = 'https://t.bilibili.com/' + item['id_str']
  author = item['modules']['module_author']
  ret['author_name'] = author['name']
  t = Time.at(author['pub_ts'].to_i()).gmtime
  ret['timestamp'] = t.strftime '%a, %d, %b %Y %H:%M:%S GMT'
  desc = item['modules']['module_dynamic']['desc']
  maj = item['modules']['module_dynamic']['major']
  ret['title'] = '【' + ret['author_name'] + '】'
  ret['desc'] = ''
  if desc && desc['text']
    ret['title'] = ret['title'] + desc['text']
    ret['desc'] = desc['text']
  end
  addition = item['modules']['module_dynamic']['additional']
  if addition
    if addition['type'] == 'ADDITIONAL_TYPE_UGC'
      ugc = addition['ugc']
      ret['desc'] = ret['desc'] + '<br><img src="' + ugc['cover'] + '" referrerpolicy="no-referrer">'
      ret['desc'] = ret['desc'] + '<br>标题：' + s(ugc['title'])
      ret['desc'] = ret['desc'] + '<br>时长：' + s(ugc['duration'])
      ret['desc'] = ret['desc'] + '<br>：' + s(ugc['desc_second'])
      ret['desc'] = ret['desc'] + '<br>链接：' + url2Href(ugc['jump_url'])
    elsif addition['type'] == 'ADDITIONAL_TYPE_GOODS'
      goods = addition['goods']
      ret['desc'] = ret['desc'] + '<br>' + s(goods['head_text'])
      for g in (goods['items'] || [])
        ret['desc'] = ret['desc'] + '<br><img src="' + s(g['cover']) + '" referrerpolicy="no-referrer">'
        ret['desc'] = ret['desc'] + '<br>商品：' + s(g['name']) + ' ' + s(g['price'])
        ret['desc'] = ret['desc'] + '<br>链接：' + url2Href(g['jump_url'])
      end
    else
      ret['desc'] = ret['desc'] + '<br>未处理类型：' + addition['type']
    end
  end
  if maj
    #binding.pry
    if maj['type'] == 'MAJOR_TYPE_ARCHIVE'
      arch = maj['archive']
      ret['desc'] = ret['desc'] + '<br><img src="' + arch['cover'] + '" referrerpolicy="no-referrer">'
      ret['desc'] = ret['desc'] + '<br>标题：' + s(arch['title'])
      ret['desc'] = ret['desc'] + '<br>时长：' + s(arch['duration_text'])
      ret['desc'] = ret['desc'] + '<br>弹幕：' + s(arch['stat']['danmaku']) + ' 播放:' + s(arch['stat']['play'])
      ret['desc'] = ret['desc'] + '<br>简介：' + s(arch['desc'])
      ret['desc'] = ret['desc'] + '<br>链接：' + url2Href(arch['jump_url'])
      if ret['title'] == '【' + ret['author_name'] + '】'
        ret['title'] = ret['title'] + s(arch['title'])
      end
    elsif maj['type'] == 'MAJOR_TYPE_DRAW'
      for pic in maj['draw']['items']
        #binding.pry
        ret['desc'] = ret['desc'] + '<br><img src="' + pic['src'] + '" referrerpolicy="no-referrer">'
      end
    elsif maj['type'] == 'MAJOR_TYPE_OPUS'
      # itemOpusStyle: DRAW/ARTICLE 等已统一为 OPUS，正文在 summary，module_dynamic.desc 常为 null
      opus = maj['opus']
      summary_text = opus.dig('summary', 'text').to_s
      if ret['title'] == '【' + ret['author_name'] + '】'
        title_extra = (!opus['title'].nil? && !opus['title'].empty?) ? opus['title'] : summary_text
        ret['title'] = ret['title'] + title_extra
      end
      for pic in (opus['pics'] || [])
          ret['desc'] = ret['desc'] + '<br><img src="' + pic['url'] + '" referrerpolicy="no-referrer">'
      end
      if opus['title'] != nil && !opus['title'].empty?
          ret['desc'] = ret['desc'] + '<br>标题：' + opus['title']
      end
      ret['desc'] = ret['desc'] + '<br>链接：' + url2Href(opus['jump_url'])
      ret['desc'] = ret['desc'] + '<br>摘要：' + summary_text unless summary_text.empty?
    elsif maj['type'] == 'MAJOR_TYPE_ARTICLE'
      # 兼容未开启 itemOpusStyle 时的专栏格式
      art = maj['article']
      ret['desc'] = ret['desc'] + '<br><img src="' + s(art['covers']&.first || art['cover']) + '" referrerpolicy="no-referrer">' if art['covers'] || art['cover']
      ret['desc'] = ret['desc'] + '<br>标题：' + s(art['title'])
      ret['desc'] = ret['desc'] + '<br>简介：' + s(art['desc'])
      ret['desc'] = ret['desc'] + '<br>链接：' + url2Href(art['jump_url'])
      if ret['title'] == '【' + ret['author_name'] + '】'
        ret['title'] = ret['title'] + s(art['title'])
      end
    elsif maj['type'] == 'MAJOR_TYPE_MUSIC'
      music = maj['music']
      ret['desc'] = ret['desc'] + '<br><img src="' + music['cover'] + '" referrerpolicy="no-referrer">'
      ret['desc'] = ret['desc'] + '<br>标题：' + s(music['title'])
      ret['desc'] = ret['desc'] + '<br>链接：' + url2Href(music['jump_url'])
    else
      ret['desc'] = ret['desc'] + '<br>未处理类型：' + maj['type']
    end
  end
  if item['type'] == 'DYNAMIC_TYPE_FORWARD' && item['orig']
    subItem = parseItem item['orig']
    ret['desc'] = ret['desc'] + '<br><br>转发内容：'
    ret['desc'] = ret['desc'] + '<br>作者：' + subItem['author_name']
    ret['desc'] = ret['desc'] + '<br>' + subItem['desc']
  end
  ret
end

uri = URI('https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all?timezone_offset=-480&type=all&page=1&features=itemOpusStyle')
req = Net::HTTP::Get.new(uri)
# 不要设置 :authority/:method/:path/:scheme —— 那是 HTTP/2 伪头；
# Net::HTTP 走 HTTP/1.1，带上会被部分 CDN/网关直接 400
req['accept'] = 'application/json, text/plain, */*'
#req['accept-encoding'] = 'gzip, deflate, br'
req['accept-language'] = 'zh-TW,zh-CN;q=0.9,zh;q=0.8,en-US;q=0.7,en;q=0.6'
cookie_path = File.expand_path('cookie.txt', __dir__)
unless File.exist?(cookie_path)
  warn "missing cookie file: #{cookie_path}"
  exit 1
end
# 动态 feed 需要登录态 SESSDATA；缺少时 API 返回 code=-101 且 data=null
# 注意：SESSDATA 是 HttpOnly，浏览器控制台 document.cookie 读不到，
# 必须从 DevTools -> Network -> 任一 api.bilibili.com 请求 -> Request Headers -> Cookie 整行复制
cookie_str = File.read(cookie_path, encoding: 'UTF-8').scrub.delete_prefix("\uFEFF")
# 归一化：cookie 名/值都不含空格，因此按 ; 或任意空白切 token，
# 再过滤出合法的 name=value，可容忍：单行、多行、cURL 续行反斜杠、
# 整行 Cookie: 前缀、引号、字段前空格、结尾多余分号
# 用 map + compact 而非 filter_map，兼容 Ruby 2.6（macOS 自带版本）
cookie_str = cookie_str.split(/[;\s]+/).map { |kv|
  kv = kv.strip.sub(/\\+\z/, '').delete(%q{'"}).sub(/\Acookie:/i, '')
  name, value = kv.split('=', 2)
  next nil if name.nil? || value.nil? || value.empty? || name !~ /\A[\w\-.%]+\z/
  "#{name}=#{value}"
}.compact.join('; ')
cookie_map = cookie_str.split('; ').map { |kv| kv.split('=', 2) }.to_h

if cookie_map['SESSDATA'].to_s.strip.empty?
  warn 'cookie.txt 缺少 SESSDATA —— 这是登录态凭证（HttpOnly，document.cookie 读不到）'
  warn '获取方式：DevTools -> Network -> 任一 api.bilibili.com 请求 -> Request Headers -> Cookie，整行复制'
  exit 1
end
unless cookie_map['buvid3']
  warn 'warning: cookie.txt 缺少 buvid3，某些情况下可能被风控拦截（code=-352）'
end

req['cookie'] = cookie_str
req['dnt'] = '1'
req['origin'] = 'https://t.bilibili.com'
req['referer'] = 'https://t.bilibili.com/'
req['sec-ch-ua'] = '"Google Chrome";v="111", "Not(A:Brand";v="8", "Chromium";v="111"'
req['sec-ch-ua-mobile'] = '?0'
req['sec-ch-ua-platform'] = '"Windows"'
req['sec-fetch-dest'] = 'empty'
req['sec-fetch-mode'] = 'cors'
req['sec-fetch-site'] = 'same-site'
req['user-agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/111.0.0.0 Safari/537.36'


res = Net::HTTP.start(uri.hostname, uri.port, use_ssl: uri.scheme == 'https') { |http|
  http.request(req)
}


j = begin
  JSON.parse(res.body)
rescue JSON::ParserError => e
  warn "bili api returned non-JSON (HTTP #{res.code}): #{res.body[0, 200].gsub(/\s+/, ' ')}"
  raise e
end
#pp j

if j['code'] != 0 || j['data'].nil? || j['data']['items'].nil?
  warn "bili api error: code=#{j['code']} message=#{j['message']}"
  print JSON.generate([])
  exit 1
end

#binding.pry
ret = j['data']['items'].filter { |item| item["type"] != 'DYNAMIC_TYPE_LIVE_RCMD' }.map { |item| parseItem item }
print JSON.generate(ret)

#binding.pry

#puts res.body
