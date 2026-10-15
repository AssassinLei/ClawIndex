#!/usr/bin/env bash
# Gen2 single-host deployment. Never deletes volumes or overwrites an existing DB.
set -Eeuo pipefail
umask 077
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

fail() { printf '%s\n' "$*" >&2; exit 1; }
usage() {
  cat <<'HELP'
用法（在Gen2项目目录执行）：
  bash deploy.sh install --import-db /绝对路径/新版一致性备份.db
  bash deploy.sh install --empty
  bash deploy.sh update          # 先备份，再拉取Gen2、构建和更新两个服务
  bash deploy.sh start           # 启动或恢复已有部署
  bash deploy.sh status          # 查看服务和数据库状态
  bash deploy.sh logs            # 查看最近日志
  bash deploy.sh backup          # 一致性备份到独立备份卷
  bash deploy.sh admin 账号名    # 空库首次部署后设置管理员；不会启用推送
  bash deploy.sh stop            # 手动停止后，不会在重启时自动恢复；用start恢复
先安装Docker Engine + Compose插件，并填写.env.cloud。详见docs/Gen2-Linux部署.md。
HELP
}

[[ ${1:-} != --help && ${1:-} != -h && $# -gt 0 ]] || { usage; exit 0; }
ACTION="$1"; shift
case "$ACTION" in install|update|start|status|logs|backup|stop|admin) ;; *) usage; exit 2;; esac
ACCOUNT=""
if [[ "$ACTION" == admin ]]; then
  [[ $# -eq 1 && -n "$1" ]] || fail '请提供一个账号名，例如bash deploy.sh admin 我的账号'
  ACCOUNT="$1"; shift
fi
IMPORT=""; EMPTY=false
while (($#)); do
  case "$1" in
    --import-db) [[ $# -ge 2 ]] || fail '缺少备份文件路径'; IMPORT="$2"; shift 2;;
    --empty) EMPTY=true; shift;;
    *) fail "不支持的参数：$1";;
  esac
done
[[ "$ACTION" == install || ( -z "$IMPORT" && "$EMPTY" == false ) ]] || fail '初始化参数仅用于install'
[[ -z "$IMPORT" || "$EMPTY" == false ]] || fail '--import-db和--empty只能选择一种'
command -v docker >/dev/null || fail '请先安装Docker Engine和Compose插件，见部署说明'
command -v flock >/dev/null || fail '缺少flock，请安装util-linux'
exec 9>"$ROOT/.deploy.lock"
flock -n 9 || fail '已有部署或备份正在执行，请稍后重试'
docker info >/dev/null 2>&1 || fail '无法连接Docker。请启动Docker，并以有Docker权限的用户执行（例如sudo bash deploy.sh ...）'
docker compose version >/dev/null || fail '缺少Docker Compose插件，请按官方文档安装'
if [[ ! -f .env.cloud ]]; then
  cp .env.cloud.example .env.cloud
  chmod 600 .env.cloud
  fail '已生成.env.cloud，请填写TUSHARE_TOKEN和可选AI配置后重试同一命令'
fi
chmod 600 .env.cloud
compose() { docker compose --env-file "$ROOT/.env.cloud" -f "$ROOT/compose.yaml" "$@"; }
ops() { compose run --rm --no-deps ops "$@"; }
check_config() { compose run --rm --no-deps --entrypoint python ops -m clawindex_next.deployment config; }
db_state() {
  local rc=0
  compose run --rm --no-deps --entrypoint python ops -m clawindex_next.deployment status || rc=$?
  return "$rc"
}
running_image() {
  local cid image
  cid="$(compose ps -aq worker)"
  [[ -n "$cid" ]] || fail '没有已部署的worker，请先运行install'
  image="$(docker inspect --format '{{.Config.Image}}' "$cid")"
  export CLAWINDEX_IMAGE="$image"
}
permissions() {
  compose run --rm --no-deps --user root --entrypoint sh ops -c 'chown 10001:10001 /data /backups; for f in /data/quant_system_next.db /data/quant_system_next.db-wal /data/quant_system_next.db-shm; do if [ -e "$f" ]; then chown 10001:10001 "$f"; fi; done'
}
backup() { ops backup --output "/backups/gen2-$(date -u +%Y%m%dT%H%M%SZ)-$$.db"; }
start() {
  if ! compose up -d --wait --wait-timeout 180 worker web; then
    compose ps -a
    printf '%s\n' '启动未通过健康检查。数据卷保留；运行bash deploy.sh logs查看原因。' >&2
    return 1
  fi
  compose ps
  printf '%s\n' '页面和后台已启动。Docker开机启动启用后，服务器重启会恢复运行。访问方法见部署说明。'
}

case "$ACTION" in
  install)
    if [[ -n "$IMPORT" ]]; then
      [[ -f "$IMPORT" ]] || fail '导入文件不存在'
      [[ ! -s "${IMPORT}-wal" ]] || fail '该文件有非空WAL，请先导出一致性备份，不要直接上传运行中的数据库'
      IMPORT="$(realpath -- "$IMPORT")"
      [[ "$IMPORT" != *:* && "$IMPORT" != *,* ]] || fail '导入路径不能含冒号或逗号'
    fi
    [[ "$(git branch --show-current)" == Gen2 ]] || fail '请先切换到Gen2分支再部署'
    [[ -z "$(git status --porcelain --untracked-files=normal)" ]] || fail '请先保存本地代码改动再部署；.env.cloud和数据库不受影响'
    revision="$(git rev-parse --short=12 HEAD)"
    export CLAWINDEX_IMAGE="clawindex:gen2-$revision"
    compose config --quiet
    compose build worker
    check_config
    permissions
    rc=0; db_state || rc=$?
    if ((rc==10)); then
      if [[ -n "$IMPORT" ]]; then
        compose run --rm --no-deps --user root --volume "$IMPORT:/import/source.db:ro" --entrypoint python ops -m clawindex_next.deployment import --source /import/source.db
      elif [[ "$EMPTY" == true ]]; then
        ops init
      else
        fail '服务器尚无数据库，请明确选择--import-db（保留现有数据）或--empty（新账号从零开始）'
      fi
    elif ((rc!=0)); then
      fail '现有数据库检查失败；不会覆盖或初始化'
    elif [[ -n "$IMPORT" || "$EMPTY" == true ]]; then
      fail '服务器已有有效数据库，不会覆盖。请使用start或update'
    fi
    ops doctor
    start
    ;;
  update)
    [[ "$(git branch --show-current)" == Gen2 ]] || fail '仅在Gen2分支上执行更新'
    [[ -z "$(git status --porcelain --untracked-files=normal)" ]] || fail '项目有本地代码改动，请先妥善保存；不会强制覆盖'
    running_image
    db_state
    backup
    git fetch origin Gen2
    git merge --ff-only origin/Gen2
    revision="$(git rev-parse --short=12 HEAD)"
    export CLAWINDEX_IMAGE="clawindex:gen2-$revision"
    compose config --quiet
    compose build worker
    check_config
    db_state
    start
    ;;
  start) running_image; db_state; start;;
  status) running_image; compose ps -a; db_state;;
  logs) compose logs --tail=150 worker web;;
  backup) running_image; db_state; backup;;
  admin) running_image; db_state; ops create-user --admin -- "$ACCOUNT";;
  stop) compose stop worker web;;
esac
