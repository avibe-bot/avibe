# Windows 用户：从零开始用 WSL 运行 Avibe

> 在 Windows 上，推荐的方式是桌面 App：到 [avibe.bot](https://avibe.bot/?lang=zh#download) 下载即可。这篇文档写给明确想把 Avibe 放在 WSL 里的人，比如希望代码和编码 Agent 都留在 Linux 环境里。

这篇文档是写给 `完全没有用过 WSL` 的 Windows 用户的。

如果你看到“推荐用 WSL”，但完全不知道它去哪里安装、怎么打开、要在哪个窗口里执行命令，就按这篇文档一步一步做。

## 先说结论

如果你在 WSL 里运行 Avibe，是这样分工的：

- `Windows`：浏览器、Slack、Discord、Telegram、微信、飞书
- `WSL`：Avibe、Claude Code / Codex / OpenCode、你的代码仓库

简单理解就是：

- 你平时还是在 Windows 里点浏览器、看聊天工具
- 但真正执行命令和跑 Agent 的地方，不在 PowerShell，也不在 CMD，而是在一个 Linux 终端窗口里

这个 Linux 终端窗口，就是 WSL。

## 什么是 WSL

WSL 的全称是 `Windows Subsystem for Linux`。

你可以把它理解成：

- 你的电脑还是 Windows
- 但 Windows 里面多开了一个 Linux 命令行环境
- 你可以在这个 Linux 环境里运行很多更适合开发者工具的命令

对 Avibe 这种要调用 Python、Node、Agent CLI 的程序来说，WSL 通常比原生 Windows 更稳。

## 你会用到两个窗口

这一步非常重要，很多人第一次会搞混。

### 窗口 1：PowerShell

PowerShell 只在最开始安装 WSL 时用一下。

你可以把它理解成：

- 用来“安装 WSL 本身”的 Windows 终端

### 窗口 2：Ubuntu 终端

Ubuntu 终端才是后面真正运行 Avibe 的地方。

你可以把它理解成：

- 用来“安装和运行 Avibe”的 Linux 终端

后面看到类似下面这些命令：

```bash
curl ...
vibe
which codex
```

都应该在 `Ubuntu 终端` 里执行，不是在 PowerShell 里执行。

## 第一步：安装 WSL

### 1. 打开 PowerShell

在 Windows 里：

1. 点开始菜单
2. 搜索 `PowerShell`
3. 找到 `Windows PowerShell` 或 `PowerShell`
4. 右键
5. 选择 `以管理员身份运行`

### 2. 在 PowerShell 里执行安装命令

输入：

```powershell
wsl --install
```

这条命令的作用是：

- 安装 WSL
- 安装默认 Linux 发行版（通常是 Ubuntu）

如果系统提示你重启，就重启电脑。

### 3. 重启后再次进入 Windows

安装完成后，你的开始菜单里通常会出现：

- `Ubuntu`

如果没有看到，也可以搜索：

- `Ubuntu`
- `Windows Terminal`

## 第二步：第一次启动 Ubuntu

### 1. 打开 Ubuntu

点开始菜单，搜索并打开：

- `Ubuntu`

第一次打开时，系统会做一些初始化，这一步可能要等几十秒到几分钟。

### 2. 创建 Linux 用户名和密码

初始化完成后，Ubuntu 会提示你创建：

- 一个 Linux 用户名
- 一个 Linux 密码

这里的用户名和密码是 `WSL 里的 Linux 账号`，不是你的 Windows 账号。

比如它可能让你输入：

```text
Enter new UNIX username:
Enter new UNIX password:
```

设好之后，以后这个 Ubuntu 终端就是你的 WSL 工作环境。

## 第三步：确认你已经进入 WSL 终端

当你看到类似下面这样的提示符时，通常就说明你已经在 Ubuntu 里了：

```bash
yourname@DESKTOP-XXXX:~$
```

注意这里通常会有：

- 你的 Linux 用户名
- 一个波浪号 `~`
- 一个美元符号 `$`

这表示你现在在 Linux 终端里。

从这一步开始，后面的命令都在这个窗口里执行。

## 第四步：先更新 Ubuntu 里的基础工具

在 Ubuntu 终端里执行：

```bash
sudo apt update
sudo apt install -y curl git
```

如果系统要求输入密码，就输入你刚才创建的 `Linux 密码`。

## 第五步：准备一个工作目录

推荐把代码放在 WSL 自己的 Linux 文件系统里，而不是放在 Windows 的 `C:` 盘映射路径里。

在 Ubuntu 终端里执行：

```bash
mkdir -p ~/work
cd ~/work
```

推荐使用：

```text
/home/<你的 Linux 用户名>/work
```

不建议长期把项目放在：

```text
/mnt/c/Users/...
```

因为那样通常更慢，也更容易遇到权限和路径兼容问题。

## 第六步：安装 Avibe

现在你已经在正确的地方了。

就在 `Ubuntu 终端` 里执行：

```bash
curl -fsSL https://avibe.bot/install.sh | bash
```

开源脚本，源码在 [GitHub](https://github.com/avibe-bot/avibe/blob/master/install.sh) 上；该短链只是到这个文件的 307 重定向。

安装完成后，继续在同一个 Ubuntu 终端里执行：

```bash
vibe
```

再次强调：

- 这条命令在 `Ubuntu 终端` 里执行
- 不是在 PowerShell 里执行
- 也不是在 CMD 里执行

## 第七步：在 Windows 浏览器里打开 Web UI

Avibe 启动后，Web UI 默认地址是：

```text
http://127.0.0.1:5123
```

这时候你可以在 `Windows 的浏览器` 里打开这个地址，比如：

- Chrome
- Edge
- Firefox

直接访问：

```text
http://127.0.0.1:5123
```

如果浏览器没有自动弹出来，手动打开这个地址就行。

## 第八步：安装 Agent CLI

如果你要用 Claude Code、Codex 或 OpenCode，这些 CLI 也应该安装在 `Ubuntu 终端` 里。

不要只装在 Windows 里。

因为：

- Avibe 跑在 WSL 里
- 它只会调用 WSL 里能找到的命令

安装完成后，你可以在 Ubuntu 终端里确认：

```bash
which claude
which codex
which opencode
```

如果能看到类似下面的输出，就说明这个命令已经能在 WSL 里被找到：

```text
/home/yourname/.local/bin/codex
```

## 第九步：设置默认工作目录

在 Avibe 的 Web UI 里，把默认工作目录设置成 WSL 路径，例如：

```text
/home/yourname/work
```

不要优先设置成：

```text
C:\Users\...
```

也不要优先设置成：

```text
/mnt/c/Users/...
```

如果你一定要访问 Windows 文件，也可以临时用 `/mnt/c/...`，但不建议作为长期默认目录。

## 一个最容易理解的使用例子

你每天可以这样使用：

### 1. 打开 Ubuntu

开始菜单 -> 搜索 `Ubuntu` -> 打开

### 2. 进入项目目录

在 Ubuntu 终端里：

```bash
cd ~/work/your-project
```

### 3. 启动 Avibe

在 Ubuntu 终端里：

```bash
vibe
```

### 4. 打开浏览器

在 Windows 浏览器里访问：

```text
http://127.0.0.1:5123
```

### 5. 在聊天工具里使用

之后你就在 Slack、Discord、Telegram、微信、飞书里和 Agent 交互。

## 如何再次打开 WSL

以后每次想继续使用，不需要重新安装。

你只需要：

1. 打开开始菜单
2. 搜索 `Ubuntu`
3. 打开 Ubuntu
4. 在里面执行：

```bash
cd ~/work/你的项目
vibe
```

## 常见问题

### 1. 我应该在哪个窗口里执行 `vibe`？

答：在 `Ubuntu 终端` 里，不是在 PowerShell 里。

### 2. 我应该在哪个窗口里执行 `curl ... | bash`？

答：在 `Ubuntu 终端` 里，不是在 PowerShell 里。

### 3. PowerShell 还要不要继续用？

只在最开始安装 WSL 时需要。

安装完成后，日常使用 Avibe 基本都在 Ubuntu 终端里。

### 4. 浏览器打不开 `127.0.0.1:5123` 怎么办？

先确认 Ubuntu 终端里 `vibe` 还在运行。

如果 `vibe` 已经退出，浏览器当然打不开。

你可以先在 Ubuntu 终端里看一下：

```bash
vibe status
```

### 5. Web UI 里安装 Agent 失败怎么办？

如果你是在 Windows 浏览器里操作，而 Avibe 跑在 WSL 里，有时本地安全校验可能让某些安装按钮失败。

这时不要卡在按钮里，直接回到 `Ubuntu 终端` 手动安装对应的 CLI，然后回到 Web UI 填路径或重新检测即可。

### 6. 我必须懂 Linux 才能用吗？

不用。

你只需要会做这几件事：

- 打开 Ubuntu
- `cd` 进入目录
- 运行 `vibe`
- 在浏览器里打开 `http://127.0.0.1:5123`

这就够开始用了。

## 快速检查清单

如果下面这些都成立，说明你的 WSL 方案已经跑通了：

- 你能在开始菜单里打开 `Ubuntu`
- Ubuntu 打开后能看到类似 `yourname@DESKTOP-XXXX:~$`
- 你能在 Ubuntu 终端里运行 `vibe`
- 你能在 Windows 浏览器里打开 `http://127.0.0.1:5123`
- `which codex` / `which claude` / `which opencode` 能在 Ubuntu 里找到对应命令

## 官方参考

如果你想看微软官方文档，可以参考：

- WSL 安装：<https://learn.microsoft.com/windows/wsl/install>
- WSL 基础命令：<https://learn.microsoft.com/windows/wsl/basic-commands>
- WSL 开发环境说明：<https://learn.microsoft.com/windows/wsl/setup/environment>

## 一句话总结

在 WSL 里运行 Avibe，从头到尾就是这几步：

- 先在 Windows 里安装 WSL
- 再打开 Ubuntu
- 在 Ubuntu 终端里安装并运行 Avibe
- 在 Windows 浏览器里访问 `http://127.0.0.1:5123`

如果你并不需要 Linux 环境，直接用桌面 App 就行。
