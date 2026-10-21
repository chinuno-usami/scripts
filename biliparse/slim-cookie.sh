#!/bin/sh
# slim-cookie.sh —— 精简 cookie.txt，只保留 bili.rb 真正需要的字段
#
# 用法:
#   ./slim-cookie.sh                 # 就地精简 ./cookie.txt（自动备份 cookie.txt.bak）
#   ./slim-cookie.sh -n              # 只看结果，不写文件
#   ./slim-cookie.sh -k "SESSDATA buvid3 buvid4 bili_jct"   # 自定义保留字段
#   ./slim-cookie.sh /path/to/cookie.txt
#
# 输入容忍：单行、多行、Copy as cURL 整行/续行、带 BOM、字段前空格、引号、结尾多余分号

set -eu

KEEP=${KEEP:-"SESSDATA buvid3 buvid4"}
DRY_RUN=0
BACKUP=1
FILE=

usage() {
  cat <<'EOF'
用法: slim-cookie.sh [-n] [-K] [-k "字段1 字段2 ..."] [cookie文件]

  -n, --dry-run   只打印精简结果，不写文件
  -K, --no-backup 不留 .bak 备份
  -k, --keep      保留字段（空格分隔），默认 "SESSDATA buvid3 buvid4"
  -h, --help      显示帮助

说明: SESSDATA 是唯一登录凭证（HttpOnly，document.cookie 读不到）；
      buvid3/buvid4 是设备指纹，留着可降低被风控(-352)的概率。
EOF
}

while [ $# -gt 0 ]; do
  case $1 in
    -n|--dry-run) DRY_RUN=1; shift ;;
    -K|--no-backup) BACKUP=0; shift ;;
    -k|--keep) KEEP=${2:?--keep 需要一个参数}; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    --) shift; break ;;
    -*) echo "未知选项: $1" >&2; usage >&2; exit 2 ;;
    *) FILE=$1; shift ;;
  esac
done

if [ -z "$FILE" ]; then
  FILE=$(cd "$(dirname "$0")" && pwd)/cookie.txt
fi
[ -f "$FILE" ] || { echo "✗ 找不到 cookie 文件: $FILE" >&2; exit 1; }
[ -s "$FILE" ] || { echo "✗ cookie 文件是空的: $FILE" >&2; exit 1; }

CAT_AWK='
# 把所有 token 收集起来（按 ; 或空白切分），只挑出 KEEP 里的字段
{
  line = $0
  sub(/\r$/, "", line)
  gsub(/\\+$/, "", line)          # 去掉 cURL 续行反斜杠
  all = (NR == 1 ? line : all " " line)
}
END {
  for (i = 1; i <= nkeep; i++) want[K[i]] = 1

  n = split(all, tok, /[ \t;]+/)
  for (i = 1; i <= n; i++) {
    t = tok[i]
    if (length(t) < 3) continue
    if ((t ~ /^".*"$/) || (t ~ /^'"'"'.*'"'"'$/)) t = substr(t, 2, length(t) - 2)

    eq = index(t, "=")
    if (eq < 2) continue

    name = substr(t, 1, eq - 1)
    val  = substr(t, eq + 1)
    sub(/^[^A-Za-z0-9_.%-]+/, "", name)   # 去掉 BOM / 引号 / "cookie:" 之类的前缀残留
    sub(/^cookie:$/, "", name)

    if (name !~ /^[A-Za-z0-9_.%-]+$/) continue
    if (val == "") continue

    total++
    if (!(name in want)) { dropped = dropped (dropped == "" ? "" : ", ") name; continue }
    store[name] = val            # 同名字段：后出现的覆盖前面的
    found[name] = 1
  }

  for (i = 1; i <= nkeep; i++) {
    name = K[i]
    if (name in found) {
      out = out (out == "" ? "" : "; ") name "=" store[name]
      keptnames = keptnames (keptnames == "" ? "" : ", ") name
    } else {
      missing = missing (missing == "" ? "" : ", ") name
    }
  }

  printf "%s", out
  printf "TOTAL\t%d\nKEPT\t%s\nDROPPED\t%s\nMISSING\t%s\n", total, keptnames, dropped, missing > "/dev/stderr"
}
'

STATS=$(mktemp "${TMPDIR:-/tmp}/slimcookie.stats.XXXXXX")
trap 'rm -f "$STATS" "${NEWFILE:-}"' EXIT INT HUP TERM

NEW=$(awk -v keep="$KEEP" -v nkeep=0 '
  BEGIN { nkeep = split(keep, K, " ") }
  '"$CAT_AWK" "$FILE" 2>"$STATS")

stat_of() { awk -F'\t' -v k="$1" '$1 == k { print $2 }' "$STATS"; }
TOTAL=$(stat_of TOTAL)
KEPT=$(stat_of KEPT)
DROPPED=$(stat_of DROPPED)
MISSING=$(stat_of MISSING)

if ! printf '%s' "$NEW" | grep -q 'SESSDATA=[^;[:space:]]'; then
  echo "✗ 精简结果里没有 SESSDATA，拒绝写入（原文件未改动）" >&2
  echo "  这是登录态凭证，且是 HttpOnly，浏览器控制台 document.cookie 读不到。" >&2
  echo "  获取方式：DevTools -> Network -> 任一 api.bilibili.com 请求 -> Request Headers -> Cookie，整行复制" >&2
  exit 1
fi

OLD_BYTES=$(wc -c < "$FILE" | tr -d ' ')
NEW_BYTES=$(printf '%s\n' "$NEW" | wc -c | tr -d ' ')

echo "文件      : $FILE"
echo "原始      : ${TOTAL:-0} 个字段, ${OLD_BYTES} 字节"
echo "保留      : ${KEPT}"
echo "删除      : ${DROPPED:-（无）}"
[ -n "${MISSING:-}" ] && echo "未找到    : ${MISSING}（可选，缺了通常也能跑）"
echo "结果      : ${NEW_BYTES} 字节"

if [ "$DRY_RUN" = 1 ]; then
  echo
  echo "--- dry-run，未写入 ---"
  printf '%s\n' "$NEW"
  exit 0
fi

if [ "$(printf '%s\n' "$NEW")" = "$(cat "$FILE")" ]; then
  echo "已经是最精简状态，无需修改。"
  exit 0
fi

NEWFILE=$(mktemp "${TMPDIR:-/tmp}/slimcookie.new.XXXXXX")
printf '%s\n' "$NEW" > "$NEWFILE"
chmod 600 "$NEWFILE"
if [ "$BACKUP" = 1 ]; then
  cp -p "$FILE" "$FILE.bak"
  chmod 600 "$FILE.bak"
  echo "备份      : $FILE.bak"
fi
mv "$NEWFILE" "$FILE"
NEWFILE=
echo "✓ 已写入（权限 600）"

# 若在 git 仓库里且 cookie 未被忽略，提醒一下
if command -v git >/dev/null 2>&1; then
  DIR=$(dirname "$FILE")
  if git -C "$DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    if ! git -C "$DIR" check-ignore -q "$FILE" 2>/dev/null; then
      echo "⚠ $FILE 未被 .gitignore 忽略 —— SESSDATA 等同于密码，别提交进仓库" >&2
    fi
  fi
fi
