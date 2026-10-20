#!/usr/bin/env bash
# 在影像服务器本机执行；兼容 Bash 4+。
set -Eeuo pipefail
umask 077
APP_DIR="${DATAFLY_DIR:-/data/datafly}"
APP_JAR="$APP_DIR/datafly-admin.jar"
BACKUPS="$APP_DIR/patch-backups"
STAGE=''
LOG=''
cleanup() { if [[ -n "$STAGE" ]]; then rm -f -- "$STAGE"; fi; }
trap cleanup EXIT
trap 'printf "操作失败（第 %s 行）。请查看输出与当前服务状态；不会继续替换或启动。\n" "$LINENO" >&2' ERR

validate() {
  local file="$1"
  [[ -f "$file" && -r "$file" && -s "$file" ]] || { echo "补丁不存在、不可读或为空：$file" >&2; return 1; }
  command -v unzip >/dev/null || { echo '需要服务器安装 unzip，以检查 JAR 完整性。' >&2; return 1; }
  unzip -tqq "$file" || { echo 'JAR 完整性检查失败。' >&2; return 1; }
  unzip -Z1 "$file" | awk '$0 == "META-INF/MANIFEST.MF" { found=1 } END { exit !found }' || {
    echo '文件缺少 META-INF/MANIFEST.MF。' >&2; return 1;
  }
  sha256sum -- "$file"
}

service() { (cd "$APP_DIR" && bash ./datafly.sh "$1"); }

deploy() {
  local source="$1" label="$2" backup answer stamp
  command -v flock >/dev/null || { echo '需要服务器安装 flock（通常包含在 util-linux）。' >&2; return 1; }
  [[ -d "$APP_DIR" && -w "$APP_DIR" && -f "$APP_JAR" && -f "$APP_DIR/datafly.sh" ]] || {
    echo "请检查部署目录、旧 JAR、datafly.sh 以及目录写权限：$APP_DIR" >&2; return 1;
  }
  # 独占锁覆盖检查、确认、停止、替换及启动全过程。
  exec 9>"$APP_DIR/.datafly-update.lock"
  flock -n 9 || { echo '另一个更新工具正在运行。' >&2; return 1; }
  validate "$source"
  mkdir -p -- "$BACKUPS"
  stamp="$(date +%Y%m%d_%H%M%S)_$$"
  LOG="$BACKUPS/operation-$stamp.log"
  exec > >(tee -a "$LOG") 2>&1
  printf '\n操作：%s\n服务器：%s\n部署目录：%s\n来源：%s\n日志：%s\n' "$label" "$(hostname)" "$APP_DIR" "$source" "$LOG"
  echo '请核实以上主机和部署目录，操作将停止并重启影像后端。'
  read -r -p '输入 UPDATE 确认，其他输入取消：' answer
  [[ "$answer" == UPDATE ]] || { echo '已取消。'; return 0; }
  STAGE="$(mktemp "$APP_DIR/.datafly-stage.XXXXXXXX")"
  cp -- "$source" "$STAGE"
  validate "$STAGE"
  # 保留原有权限，避免上传文件权限影响服务账号读取。
  chmod --reference="$APP_JAR" "$STAGE"
  if [[ "$(stat -c %u "$STAGE")" != "$(stat -c %u "$APP_JAR")" || "$(stat -c %g "$STAGE")" != "$(stat -c %g "$APP_JAR")" ]]; then
    chown --reference="$APP_JAR" "$STAGE"
  fi
  backup="$BACKUPS/datafly-admin-$stamp.jar"
  cp -p -- "$APP_JAR" "$backup"
  cmp -s -- "$APP_JAR" "$backup"
  echo "旧包已备份：$backup"
  echo '正在停止服务……'
  service stop
  service status || true
  echo '请根据以上状态确认服务已停止。若仍运行，请取消并检查停止脚本。'
  read -r -p '确认已停止，输入 STOPPED 继续替换：' answer
  [[ "$answer" == STOPPED ]] || { echo '已停止本次操作，旧包未替换；请人工核实服务是否需要启动。'; return 0; }
  mv -f -- "$STAGE" "$APP_JAR"
  STAGE=''
  echo '正式包已替换，正在启动……'
  service start
  echo '启动命令已返回，当前状态如下：'
  service status
  echo "请继续检查应用启动日志并访问影像功能。仅凭启动命令和状态不能确认业务正常。"
  echo "如需回退：重新运行本工具，选择 2，输入 $backup"
}

main() {
  local choice source
  if [[ "${1:-}" == '--check' ]]; then
    [[ $# == 2 ]] || { echo '用法：bash datafly-update.sh --check /tmp/datafly-admin.jar'; return 1; }
    validate "$2"
    return
  fi
  printf '影像后端补丁工具\n目录：%s\n1. 更新补丁\n2. 从备份回退\n3. 查看服务状态\n4. 列出备份\n0. 退出\n' "$APP_DIR"
  read -r -p '请选择：' choice
  case "$choice" in
    1) read -r -p '补丁路径 [/tmp/datafly-admin.jar]：' source
       deploy "${source:-/tmp/datafly-admin.jar}" '更新后端补丁' ;;
    2) if [[ -d "$BACKUPS" ]]; then find "$BACKUPS" -maxdepth 1 -type f -name 'datafly-admin-*.jar' -print | sort; fi
       read -r -p '请输入上方备份文件的完整路径：' source
       [[ "$source" == "$BACKUPS/"* && -f "$source" && ! -L "$source" && "$(dirname -- "$source")" == "$BACKUPS" ]] || { echo '请选择备份目录中的 JAR。'; return 1; }
       deploy "$source" '回退后端补丁' ;;
    3) service status ;;
    4) if [[ -d "$BACKUPS" ]]; then find "$BACKUPS" -maxdepth 1 -type f -name 'datafly-admin-*.jar' -print | sort; else echo '暂无备份。'; fi ;;
    0) return ;;
    *) echo '无效选项。'; return 1 ;;
  esac
}
main "$@"
