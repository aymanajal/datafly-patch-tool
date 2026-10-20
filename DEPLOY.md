# 部署说明

推荐先在测试环境验证，再在正式环境按相同方法部署。下面不包含真实现场 IP；地址、后端目录和业务端口都以实际环境为准。工具默认业务目录 `/data/datafly`、后端端口 `2049`，测试环境可能不同。

## 1. 准备文件

把仓库源码上传到 `/opt/datafly-patch-tool`，使用原来管理影像服务的账号执行。无需上传业务 JAR 到 GitHub。

```bash
cd /opt/datafly-patch-tool
python3 --version
command -v python3
command -v java
ss -lntp | grep ':9081 '
```

9081 无输出通常表示没有监听，不能据此确认网络连通。已有监听时先核实服务，不要停止未知进程。

## 2. 初始化密码

```bash
python3 server.py --init
```

设置至少 12 位的管理密码。输入不回显，只保存加盐 PBKDF2 摘要。已有 `config.json` 不会被初始化命令覆盖。不要提交该文件，也不要把内容整体贴进公开日志。

## 3. 按环境填写配置

```bash
vi /opt/datafly-patch-tool/config.json
```

保留 `salt`、`password_hash`，修改以下字段：

| 字段 | 含义 |
| --- | --- |
| `bind` | 工具监听地址；直连时填服务器 IP，默认仅本机 `127.0.0.1` |
| `port` | 工具端口，默认 `9081`，不是影像后端端口 |
| `target_label` | 页面服务器标识，建议包含“测试”或“正式” |
| `app_dir` | 后端 JAR 与 `datafly.sh` 所在目录 |
| `app_port` | 后端 Java 实际监听端口，用于检查 |
| `allowed_peers` | 允许访问的来源 IP 列表，例如本机地址和运维电脑 IP |
| `base_path` | 默认 `/patch-tool/`，必须以 `/` 开头和结尾 |
| `secure_cookie` | HTTPS 入口设为 `true`；直接 HTTP 访问时为 `false` |

配置只在工具启动时读取，修改后需要重启工具。正在执行更新时不要重启。

Mac 查询访问某台服务器的出口网卡与 IP：

```bash
route -n get 服务器IP | grep interface
ipconfig getifaddr en0
```

最后一条用实际网卡替换 `en0`。如果经过 NAT、VPN 或代理，以服务器看到的实际来源 IP 为准。`allowed_peers` 是工具自己的精确 IP 限制，不会自动修改服务器防火墙。

## 4. 只读检查

```bash
python3 server.py --check
```

确认输出的服务器标识、部署目录、后端端口正确，且存在目标 PID、`port_open` 为 `true`。先处理检查发现的问题，再执行更新。

找实际监听端口：

```bash
ss -lntp | grep java
```

核对某个 PID 的启动参数和目录（贴出前遮掉可能的秘密）：

```bash
tr '\0' ' ' < /proc/目标PID/cmdline
echo
readlink -f /proc/目标PID/cwd
readlink -f /proc/目标PID/exe
```

目标 PID 需替换成数字。工具结合 Java 可执行文件、JAR 参数和实际目录识别进程，不只靠模糊名称匹配。

现有脚本可能存在 `-jar -D...` 参数顺序。工具拦截此写法，先备份脚本，再将 JVM 属性放到 `-jar` 前，保留其余原参数。例如：

```bash
nohup java $JVM_OPTS -Djava.util.Arrays.useLegacyMergesort=true -jar "$AppName" > /dev/null 2>&1 &
```

参考 [Java 官方命令说明](https://docs.oracle.com/en/java/javase/17/docs/specs/man/java.html#synopsis)。仅修改脚本不改变已有进程参数。工具已兼容现场旧进程在 `-jar` 后保留 `-D` 参数的识别方式，详见过程记录。不要为校验工具而直接重启正式后端。

## 5. 前台验证网页

```bash
python3 server.py
```

保持终端运行，浏览器打开 `http://服务器IP:9081/patch-tool/`，使用刚设置的密码登录。先确认环境信息和状态，暂不执行更新。

Mac 测连通：

```bash
nc -vz -G 5 服务器IP 9081
```

必须先有服务监听才能有效验证端口。`succeeded` 表示连通；拒绝连接需检查监听和拒绝规则；超时需排查网络策略。

- 返回 `403 / 此地址不允许访问更新工具`：请求已到工具，检查 `allowed_peers`。
- 返回“访问路径不正确”：核对 URL 与 `base_path`，尤其是末尾 `/`。
- 登录成功但状态不正确：检查配置的后端端口与进程工作目录。

## 6. 改为 systemd 后台运行

确认没有更新任务，在前台终端按 Ctrl+C 停止网页工具。停止工具不主动停止已运行的影像 Java。

检查 `deploy/datafly-patch-web.service`：默认路径 `/opt/datafly-patch-tool`、Python `/usr/bin/python3`、执行账号 `root`。按实际账号、Python 路径修改；应与原服务运行身份一致。若 Java 依赖交互 shell 的环境变量，在服务模板设置实际 `JAVA_HOME`、`PATH`。

```bash
cp /opt/datafly-patch-tool/deploy/datafly-patch-web.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now datafly-patch-web
systemctl status datafly-patch-web --no-pager
```

应为 `active (running)`，且 `Loaded` 显示 `enabled`。刷新网页并重新登录。配置文件不变，测试和正式各自保留自己的目录、端口与密码。

模板使用 `KillMode=process`，避免停止工具时连带停止工具启动的 Java；这也意味着不能依赖停止工具来停止业务进程。更新期间不要重启工具。若工具异常退出，需核实实际服务状态。

## 7. 可选 Nginx

电脑可以直连工具时，无需 Nginx。本次正式环境采用直连，没有修改原 Nginx。

需要代理时才使用 `deploy/nginx-location.conf`，根据同机或分机调整上游地址，将实际代理来源 IP 加入 `allowed_peers`。默认示例为同机代理。保留 `/patch-tool/` 前缀，上传体积上限与工具一致，覆盖过大的请求体缓冲。

先备份目标配置文件，再加入 location，执行 Nginx 配置检查；仅检查成功后 reload。不要覆盖原有业务 location。

## 8. 首次更新

先在测试环境用真实补丁验证，再在正式维护窗口执行。流程见 [日常使用](docs/OPERATIONS.md)。部署网页工具、预检查、登录和上传文件，都不等于已经更新正式后端。
