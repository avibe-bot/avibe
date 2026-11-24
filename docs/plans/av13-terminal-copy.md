# AV-13 terminal copy: `vibe remote rebind` and the commands around it

Every sentence a person reads at the terminal, with the state or code that
produces it. The English blocks are generated from `vibe/remote_rebind.py`; the
terminal prints English only (Product, November 21). The Chinese under each is
the explanation the public Chinese docs give for that prompt, not something the
command prints. `{…}` is filled in at run time. Nothing here prints an
installation id, a device key, a callback proof or the reset confirmation.

Marks say who decided what.

## How the command runs

`vibe remote rebind` takes no arguments. It prints section B, then:

| State of this computer | What the command does | Sections |
|---|---|---|
| bound, or bound and refused by avibe.bot | explains "Bind remote access again" (browser), then offers "Bind as a new computer" | C, D, then F1, E |
| binding record damaged | offers "Bind as a new computer" only | C4, D, then F1, E |
| not bound | binds it: link, wait, accept account and domain | E |
| an earlier attempt is unfinished | one bounded step of that same attempt; never a second attempt beside it | H1 to H4 |
| a reset is unfinished | one bounded step of that reset, then binds | H5 to H7, then F1, E |
| a reset cannot continue | asks for a new reset (H8a), or says it cannot (H8b), or asks whether to complete an earlier binding instead (H9) | H8, H9 |

At a terminal the reset is offered on local state and a typed `yes`: the terminal
cannot ask avibe.bot to bind a bound computer again (that is the browser way),
so it never sees avibe.bot's refusals for that case and cannot say whose Avibe
it is. Every question declines on Enter, on end of input and on Ctrl+C.

## A. Where it will not run

**A1** · not a terminal on input and output, or started by an Agent, a Task or a Watch (`local_operator_required`). Printed on stderr; nothing was read.

```
This command asks questions and changes which Avibe this computer is bound to.
Run it by hand in a terminal on this computer; an SSH session works.
It does not run from an Agent, a Task, a script or a pipe.
Nothing changed.
```

中文说明：

```
这条命令会提问，并且会改变这台电脑绑定到哪个 Avibe。
请在这台电脑的终端里手动运行，SSH 登录也可以。
它不能由 Agent、定时任务、脚本或管道运行。
什么都没有改变。
```

## B. The state, printed first every time

`{state}` in the binding line:

| Local state | Printed | 中文 |
|---|---|---|
| `unpaired` | `not bound` | 未绑定 |
| `bound` | `bound` | 已绑定 |
| `binding_rejected` | `refused by avibe.bot` | avibe.bot 不再接受 |
| `pairing_recovery_required` | `an earlier attempt is unfinished` | 上一次绑定还没有完成 |
| `resetting` | `a reset is unfinished` | 重置还没有完成 |
| `reset_repair_required` | `a reset cannot finish` | 重置无法完成 |
| `damaged` | `the binding record on this computer is damaged` | 这台电脑上的绑定记录已损坏 |

**B1** · always. The domain line only when one is saved; the tunnel line is one of the two.

```
Remote access on this computer
  Domain:  {url}
  Binding: {state}
  Tunnel:  running
  Tunnel:  stopped
```

中文说明：

```
这台电脑的远程访问
  域名：{url}
  绑定：{state}
  隧道：运行中 / 已停止
```

**B3** · reviewer: not an either/or · under the lines when the state is `binding_rejected`; the last sentence only when the tunnel process is still running

```
avibe.bot has refused this computer's binding at least twice in a row.
This Avibe may have been deleted on avibe.bot, or bound again from another
computer. This computer cannot tell why.
The tunnel is still running. That does not mean the binding is accepted.
```

中文说明：

```
avibe.bot 已经连续至少两次拒绝这台电脑的绑定。
这个 Avibe 可能在 avibe.bot 上被删除了，也可能被另一台电脑重新绑定了；这台电脑看不出原因。
隧道还在运行，但这不代表绑定仍然被接受。
```

**B4** · the settings file could not be read cleanly (local config recovery refusal). The command stops.

```
Avibe could not read its settings file cleanly, and will not change the
binding on top of that. Nothing changed.
Run this, fix what it reports, then run vibe remote rebind again:
  vibe doctor
```

中文说明：

```
Avibe 没能完整读取它的设置文件，不会在这种情况下更改绑定。什么都没有改变。
请运行 vibe doctor，按提示修复后再运行 vibe remote rebind。
```

**B5** · a state this command does not know. It stops.

```
Avibe could not tell what state this computer's binding is in. Nothing changed.
To look again:
  vibe remote rebind
(code: {code})
```

中文说明：

```
Avibe 判断不出这台电脑的绑定处于什么状态。什么都没有改变。再看一次：vibe remote rebind（代码：{code}）
```

**B6** · lead: said once here, not a dialogue; third sentence decided by Product November 22 · under the state lines when avibe.bot answered an earlier "Bind remote access again" with a final refusal (`reason_code: issuer_replay_refused`). The state shown is the ordinary one and the ordinary flow follows. How a person gets here: they approved binding again in the browser, the computer lost the reply, and avibe.bot then refused the saved request for good.

```
avibe.bot has refused an earlier attempt to bind this computer, for good.
It is not tried again. It may still have taken effect on avibe.bot. If remote
access stops working, bind again.
```

中文说明：

```
avibe.bot 已经彻底拒绝了之前的一次绑定，不会再重试。它仍有可能已经在 avibe.bot 上生效；如果远程访问不能用了，请重新绑定。
```

## C. The two ways, shown when the computer is bound

**C1** · state `bound` or `binding_rejected`

```
There are two ways to bind this computer again.
```

中文说明：

```
重新绑定这台电脑有两种方式。
```

**C2** · Product: decided November 21; the effects sentence changed by Product November 24 · the browser way; each paragraph is indented under the title. The last paragraph is Product's sentence.

```
1. Bind remote access again
For the Avibe this computer is bound to now. It is done in a browser that
opens Avibe at this computer's own address, on the Devices page.
On a server with no browser, forward the port from your own computer:
    ssh -L {port}:127.0.0.1:{port} <user>@<this-server>
then open http://127.0.0.1:{port} there and go to Devices.
After binding again, devices that are signed in reconnect by themselves.
Pages open on this computer are not affected.
It does not change the tunnel credential.
If someone may have copied this computer's Avibe data as a whole, delete this
Avibe at avibe.bot, bind it again, then take your name back.
```

中文说明：

```
1. 重新绑定远程访问
针对这台电脑现在绑定的 Avibe。要在浏览器里完成：用浏览器打开这台电脑本机地址上的 Avibe，进入「设备」页。
服务器上没有浏览器时，从你自己的电脑做 SSH 端口转发：ssh -L {port}:127.0.0.1:{port} <用户>@<这台服务器>，然后在你的电脑上打开 http://127.0.0.1:{port}，进入「设备」页。
重新绑定后，已登录的设备会自动重新连上。在这台电脑上打开的页面不受影响。它不会更换隧道凭证。
如果这台电脑上的 Avibe 数据可能被人整个复制走过，请在 avibe.bot 上删除这个 Avibe，重新绑定后再把名字拿回来。
```

**C3** · the second way; the lists in D follow directly

```
2. Bind as a new computer
```

中文说明：

```
2. 作为一台新电脑重新绑定
```

**C4** · instead of C1 to C3 when the binding record on this computer is damaged; also before D when leftover records stop the link

```
The record of this computer's binding is damaged, so it cannot be bound again
as it is. It can be bound as a new computer.
```

中文说明：

```
这台电脑上的绑定记录已损坏，没法照原样重新绑定。可以把它作为一台新电脑重新绑定。
```

## D. Bind as a new computer

**D1** · Product: decided, with the last paragraph in this wording · the computer is bound. Printed only after `reset_interactively` has accepted the state, before anything is sealed. `{host}` is the saved domain, or "the current domain". The Health line only when Health data exists on this computer.

```
Bind as a new computer
This computer forgets the Avibe it is bound to. After that it can be bound as a
new computer, to any account.
You lose:
  - Remote access through {host} stops at once.
  - Every device signed in from elsewhere is signed out, and notifications to
    those devices stop.
  - Everyone you gave access to loses it. The new binding starts with its
    owner only.
  - Every link already sent (Show Pages, files) stops working.
  - Health data already synced is deleted; connect Health again on the iPhone.
  - {host} does not move with this computer.
    How to take a name back is shown after this step.
You keep:
  - Everything on this computer: sessions, files, Agents and settings.
    Pages open on this computer keep working.
This does not change:
  - The Avibe on avibe.bot is not deleted. If it still exists it keeps its
    name, and it can no longer reach this computer. If it belongs to another
    account, that account is not told.
  - No credential is changed: not the tunnel credential, not model keys, not
    the Vault.
If someone may have copied this computer's Avibe data as a whole, also delete
the old Avibe at avibe.bot: until then, whoever holds the copy can still use it.
This cannot be undone.
Type yes to bind as a new computer. Press Enter to leave everything as it is: 
```

中文说明：

```
作为一台新电脑重新绑定
这台电脑会忘掉它现在绑定的 Avibe，之后可以作为一台新电脑绑定到任何账号。
你会失去：
  - 通过 {host} 的远程访问立即停止。
  - 所有远程设备都会退出登录，发给这些设备的通知也会停止。
  - 你授权过的人都会失去访问权限；新的绑定一开始只有所有者自己。
  - 已经发出去的链接（展示页、文件）全部失效。
  - 已同步的健康数据会删除，要在 iPhone 上重新连接健康。
  - {host} 不会跟着这台电脑走。怎样把名字拿回来，这一步之后会显示。
你会保留：
  - 这台电脑上的一切：会话、文件、Agent 和设置。在这台电脑上打开的页面照常可用。
这一步不会改变：
  - avibe.bot 上的那个 Avibe 不会被删除。它如果还在，名字仍然归它，只是再也连不到这台电脑。如果它属于别的账号，对方不会收到任何通知。
  - 不会更换任何凭证：隧道凭证、模型密钥、Vault 都不变。
如果这台电脑上的 Avibe 数据可能被人整个复制走过，还要在 avibe.bot 上删除原来的 Avibe：在那之前，拿到副本的人仍然可以用它。
这一步不能撤销。
输入 yes 作为一台新电脑重新绑定；直接按回车则一切保持原样。
```

**D1u** · new since the first draft · the same question when the computer is **not** bound (offered after the link was refused, see G2). These lines replace the intro, the first four "You lose" lines and the "does not change" list; the rest is D1.

```
This computer is not bound now. It forgets what is left on it from earlier
bindings, and can then be bound as a new computer, to any account.
  - If this computer was bound to an Avibe before, it can no longer be bound
    to that one again. That Avibe's domain and the people with access to it
    do not move to the new binding.
This does not change:
  - Nothing on avibe.bot is deleted. An Avibe this computer was bound to
    before keeps its name, and its account is not told.
  - No credential is changed: not the tunnel credential, not model keys, not
    the Vault.
```

中文说明：

```
这台电脑现在没有绑定。它会忘掉以前的绑定留在电脑上的东西，之后可以作为一台新电脑绑定到任何账号。
你会失去：这台电脑以前如果绑定过某个 Avibe，就不能再绑回那一个；那个 Avibe 的域名和有权访问它的人不会转到新的绑定。
这一步不会改变：avibe.bot 上不会删除任何东西；这台电脑以前绑定过的 Avibe 名字仍然归它，它的账号不会收到通知。不会更换任何凭证。
```

**D2** · any answer but `yes`, end of input, Ctrl+C (`declined`)

```
Nothing changed. This computer's binding is as it was.
```

中文说明：

```
什么都没有改变，这台电脑的绑定和之前一样。
```

**D2r** · reviewer T-5 · the same, when the question was the new reset of H8a

```
Nothing changed. The earlier reset is still unfinished, and remote access on
this computer stays off.
```

中文说明：

```
什么都没有改变。之前那次重置仍然没有完成，这台电脑的远程访问保持关闭。
```

**D3** · `{'state':'unpaired'}`

```
Done. This computer is no longer bound to any Avibe. Everything on it is as it was.
```

中文说明：

```
完成。这台电脑不再绑定任何 Avibe，电脑上的内容原样保留。
```

**D4** · the binding changed while the question was open (`local_binding_changed`, nothing sealed)

```
This computer's binding changed while you were answering, so nothing was reset.
To see where it stands now:
  vibe remote rebind
```

中文说明：

```
在你回答期间，这台电脑的绑定发生了变化，所以没有重置。查看现在的状态：vibe remote rebind
```

**D5** · another binding or domain change holds the lock (`operation_in_progress`)

```
Another change to this computer's binding or domain is running. Nothing changed.
When it has finished, run:
  vibe remote rebind
```

中文说明：

```
这台电脑的绑定或域名正在进行另一项更改。什么都没有改变。等它完成后运行：vibe remote rebind
```

**D6** · any failure after the reset was sealed, including a tunnel that has not stopped yet

```
The reset started and did not finish. Remote access on this computer stays off
until it does. To continue it:
  vibe remote rebind
```

中文说明：

```
重置已经开始，但没有完成。完成之前，这台电脑的远程访问保持关闭。继续请运行：vibe remote rebind
```

**D7** · the reset could not start for a local reason not listed above

```
The reset could not start. Nothing changed. To try again:
  vibe remote rebind
(code: {code})
```

中文说明：

```
重置没能开始。什么都没有改变。再试一次：vibe remote rebind（代码：{code}）
```

## E. Binding the computer (the ordinary link)

**E1** · changed: says "name", and F1 is now printed before it · the computer is not bound: after D3, after a finished reset, or directly. No question is asked before the link.

```
Bind this computer

Open a link on any device, sign in to avibe.bot and name this computer's Avibe
there. This terminal then shows the account and the domain and asks you to
accept them. Nothing is bound before that.
```

中文说明：

```
绑定这台电脑
在任意设备上打开一个链接，登录 avibe.bot，在那里给这台电脑上的 Avibe 起名。之后这个终端会显示账号和域名，请你确认。确认之前不会绑定任何东西。
```

**E2** · `show_link(url)`

```
Open this link:
  {url}
Waiting for it to be approved on avibe.bot. Ctrl+C stops.
```

中文说明：

```
打开这个链接：{url}
正在等待 avibe.bot 上的确认。按 Ctrl+C 停止。
```

**E3** · Product: decided as written · `confirm(email, hostname)`; Enter, end of input and Ctrl+C decline

```
avibe.bot reports that this request was approved by:
  Account: {email}
  Domain:  {host}
This account becomes the owner of this computer's Avibe and can open
everything in it from anywhere. If the account or the domain is not what you
expect, answer no.
Bind this computer to this account at this domain? [y/N] 
```

中文说明：

```
avibe.bot 显示，确认这次请求的是：账号 {email}，域名 {host}。
这个账号会成为这台电脑上 Avibe 的所有者，可以从任何地方打开其中的一切。账号或域名和你预期的不一样，就回答 n。
把这台电脑绑定到这个账号和这个域名？[y/N]
```

**E4** · `bound`; the second line is one of the two, by whether the tunnel runs

```
Bound. This computer's Avibe is at https://{host}, owned by {email}.
Sign in at that address on your other devices.
The tunnel is not running yet. Start it with:
  vibe remote start
```

中文说明：

```
已绑定。这台电脑上的 Avibe 在 https://{host}，所有者是 {email}。
请在其他设备上打开这个地址重新登录。／隧道还没有运行，启动它：vibe remote start
```

**E5** · `declined` at E3

```
Not bound. You answered no, so nothing was bound. To try again:
  vibe remote rebind
```

中文说明：

```
没有绑定。你回答了“否”，所以什么都没有绑定。再试一次：vibe remote rebind
```

**E6** · Ctrl+C before E3 was accepted

```
Stopped. Nothing was bound; this computer is not bound. To continue:
  vibe remote rebind
```

中文说明：

```
已停止。什么都没有绑定，这台电脑现在是未绑定状态。继续请运行：vibe remote rebind
```

**E7** · Ctrl+C after E3 was accepted, with no saved attempt (with one: G4)

```
Stopped after you accepted. This computer is not bound. avibe.bot may have
recorded the request. Run this and approve once more:
  vibe remote rebind
```

中文说明：

```
在你确认之后停止了。这台电脑没有绑定；avibe.bot 那边可能已经记下了这次请求。请运行 vibe remote rebind 并再确认一次。
```

**E8** · `expired`

```
The link expired before it was approved. Nothing was bound. For a new link:
  vibe remote rebind
```

中文说明：

```
链接在确认之前过期了。什么都没有绑定。获取新链接：vibe remote rebind
```

**E9** · `already_bound`

```
This computer is already bound. Nothing changed.
```

中文说明：

```
这台电脑已经绑定了。什么都没有改变。
```

## F. Taking the old name back

**F1** · Product: 30 days, never the margin; both ways, nothing promised · printed after D3 and before E1 when this run reset the computer, so the old Avibe can be deleted before the name is chosen. `{old}` is the domain saved before the reset, or "the old name".

```
Taking the old name back

A name that is given up is kept for the account that had it for 30 days;
during that time nobody else can claim it.
The old Avibe gives up its name when it is deleted on avibe.bot. Until then
the name stays with it.
To use {old} for this computer:
delete the old Avibe on avibe.bot first. Then choose the name when avibe.bot
asks you to name this computer's Avibe, or afterwards in Devices, under
"Change the domain". avibe.bot decides whether the name can be taken; it is
not guaranteed.
```

中文说明：

```
把原来的名字拿回来
放弃的名字会为原来的账号保留 30 天，这期间别人不能领取。
原来的 Avibe 在 avibe.bot 上被删除时才放弃它的名字；在那之前名字一直归它。
想让这台电脑用回 {old}：先在 avibe.bot 上删除原来的 Avibe。然后在 avibe.bot 请你给这台电脑上的 Avibe 起名时选这个名字，或者之后在「设备」页的「更改域名」里改。能不能拿回由 avibe.bot 决定，不保证一定可以。
```

**F2** · after E4 when this run did not reset anything

```
Had another name before? See "Taking the old name back" in: vibe remote rebind --help
```

中文说明：

```
之前用过别的名字？见 vibe remote rebind --help 里的 “Taking the old name back”。
```

## G. Refusals at the link step

**G1** · Product: decided November 22; the same sentence in the docs and later on the page · `active_pairing_recent`. "Quit Avibe", not "turn remote access off": the background check on that computer still counts as activity.

```
avibe.bot refused: that Avibe is running on another computer that was active in
the last 15 minutes. Nothing was bound.
To move it to this computer: quit Avibe on that computer, wait 15 minutes and
try again.
If that computer is no longer yours to reach: delete that Avibe on avibe.bot and
bind again; its access list is lost, and its name is kept for your account for
30 days.
```

中文说明：

```
avibe.bot 拒绝了：那个 Avibe 正在另一台电脑上运行，它在最近 15 分钟内还活跃。什么都没有绑定。想让它改到这台电脑上：在那台电脑上退出 Avibe，等 15 分钟后再试。那台电脑已经不在你手里的话：在 avibe.bot 上删除那个 Avibe 再重新绑定；它的访问名单会丢失，名字会为你的账号保留 30 天。
```

**G2** · reviewer: the offer stays, as a separate choice with the default no · `paired_installation_loopback_required`. The last line and then D1u follow only when `reset_interactively` admits a reset; with nothing left on the computer the command stops after the refusal.

```
avibe.bot refused: this computer has been bound before, and a computer like
that is bound from a browser at its own address, not through a link.
Nothing was bound.
Use the browser on this computer, or from another computer through SSH port
forwarding:
    ssh -L {port}:127.0.0.1:{port} <user>@<this-server>
then open http://127.0.0.1:{port} there, go to Devices and turn on remote
access.
If you cannot do that, this computer can be bound as a new computer instead.
```

中文说明：

```
avibe.bot 拒绝了：这台电脑以前绑定过，这样的电脑要在它本机地址上的浏览器里绑定，不能通过链接。什么都没有绑定。
请使用这台电脑上的浏览器，或者从另一台电脑做 SSH 端口转发，然后打开 http://127.0.0.1:{port}，进入「设备」页开启远程访问。
如果做不到，也可以把这台电脑作为一台新电脑重新绑定。
```

**G3** · `control_plane_unreachable`

```
avibe.bot could not be reached, so nothing was bound. Check this computer's
network, then run:
  vibe remote rebind
```

中文说明：

```
暂时连不上 avibe.bot，所以什么都没有绑定。检查这台电脑的网络后运行：vibe remote rebind
```

**G3b** · `rate_limited`

```
avibe.bot asked this computer to wait, so nothing was bound. Wait {seconds}
seconds, then run:
  vibe remote rebind
```

中文说明：

```
avibe.bot 要求这台电脑稍后再试，所以什么都没有绑定。等 {seconds} 秒后运行：vibe remote rebind
```

**G4** · Product: decided as written · `pairing_recovery_required` from the link step or from the reset

```
The last attempt may have completed on avibe.bot, but this computer has not
finished saving it. Do not start another one.
To continue that same attempt:
  vibe remote rebind
```

中文说明：

```
上一次绑定可能已经在 avibe.bot 上完成，但这台电脑还没有把它保存完。不要再开始一次新的绑定。继续上一次请运行：vibe remote rebind
```

**G5** · `fresh_claim_required`

```
The reply from avibe.bot was lost after you accepted, and it cannot be asked
for again. This computer is not bound. Run this and approve once more:
  vibe remote rebind
```

中文说明：

```
你确认之后，avibe.bot 的回复没有送到，而且无法重新获取。这台电脑没有绑定。请运行 vibe remote rebind 并再确认一次。
```

**G6** · `claim_in_progress`

```
Another request to bind this computer is already open, in Avibe on this
computer or in another terminal. Finish or cancel that one first.
Nothing was bound here.
```

中文说明：

```
这台电脑已经有一个绑定请求在进行（在这台电脑的 Avibe 里，或另一个终端里）。请先完成或取消那一个。这里什么都没有绑定。
```

**G7** · `name_taken`

```
That domain was taken before the binding finished. Nothing was bound.
Run this and choose another:
  vibe remote rebind
```

中文说明：

```
绑定完成之前，这个域名被别人占用了。什么都没有绑定。请运行 vibe remote rebind 并换一个。
```

**G8** · reviewer: no reset offer, no inference · `binding_unavailable` with no reason. It says what is true and stops.

```
avibe.bot refused this request and gave no reason. Nothing was bound.
To try again:
  vibe remote rebind
```

中文说明：

```
avibe.bot 拒绝了这次请求，没有给出原因。什么都没有绑定。再试一次：vibe remote rebind
```

**G8b** · Product: first two lines decided November 24; the way out is G2's, lead November 24 · `other_account`, with `confirming_account_email` from avibe.bot (the account that approved the link, never the owner). Without a valid address, G8 instead. No reset is offered: it is an ordinary refusal. How a person gets here: the computer's settings lost their binding (restored from a backup, or reinstalled with the data folder kept) while it still belongs to another account's Avibe, and the link is approved signed in as a different account.

```
The Avibe this computer used is not under {email}. Sign in to avibe.bot with the
account that owns it.
Use the browser on this computer, or from another computer through SSH port
forwarding:
    ssh -L {port}:127.0.0.1:{port} <user>@<this-server>
then open http://127.0.0.1:{port} there, go to Devices and turn on remote
access.
```

中文说明：

```
这台电脑用过的那个 Avibe 不在 {email} 名下。请用拥有它的账号登录 avibe.bot。
请使用这台电脑上的浏览器，或者从另一台电脑做 SSH 端口转发：ssh -L {port}:127.0.0.1:{port} <用户>@<这台服务器>，然后在那台电脑上打开 http://127.0.0.1:{port}，进入「设备」页开启远程访问。
```

**G9** · `local_binding_changed`

```
This computer's binding changed while this was running. Nothing more was done
here. To see where it stands now:
  vibe remote rebind
```

中文说明：

```
运行期间这台电脑的绑定发生了变化，这里没有再做任何事。查看现在的状态：vibe remote rebind
```

**G10** · any other code; the code is from the local allowlist, or `unknown`

```
avibe.bot did not complete this request. Nothing was bound. To try again:
  vibe remote rebind
(code: {code})
```

中文说明：

```
avibe.bot 没有完成这次请求。什么都没有绑定。再试一次：vibe remote rebind（代码：{code}）
```

## H. Continuing something unfinished (`binding_recovery.resume_interactively`)

**H1** · state `pairing_recovery_required` at start, before the one bounded step

```
An earlier attempt to bind this computer may have completed on avibe.bot.
Continuing that same attempt now; no new one is started.
```

中文说明：

```
之前的一次绑定可能已经在 avibe.bot 上完成。现在继续那一次，不会开始新的绑定。
```

**H2** · the step returned `bound`. The last two lines only when the tunnel is not running: completing a binding does not turn remote access back on.

```
Finished. This computer is bound: {url}
The tunnel is not running yet. Start it with:
  vibe remote start
```

中文说明：

```
已完成。这台电脑已绑定：{url}
隧道还没有运行。启动它：vibe remote start
```

**H2d** · Codex: the start hint is wrong when remote access is disabled in settings · after H2 when the tunnel is not running and `remote_access.vibe_cloud.enabled` is false in the settings file. Then the five lines in section I. How a person gets here: they, a script or an Agent set that field to false in the settings file; no page and no `vibe remote` command writes it on a bound computer.

```
Remote access is disabled in this computer's settings and stays disabled.
```

中文说明：

```
这台电脑的设置里停用了远程访问，现在仍然停用。（接着是设置项、设置文件的位置和 vibe remote start）
```

**H3** · reviewer T-1: `idle` is never "Finished" · the step found nothing to continue (`idle`): for example the saved request had expired and the backend settled it. Section B is printed again for the state as it is now, and the ordinary flow for that state follows. How a person gets here: they approved a bind, it was cut short, and they came back after the key had expired.

```
Nothing is left to continue: that work is no longer saved on this computer,
and this command did not complete it. Where this computer stands now:
```

中文说明：

```
已经没有可以继续的了：那件事不再保存在这台电脑上，这条命令也没有把它完成。这台电脑现在的状态：（重新显示 B）
```

**H4** · `pairing_recovery_required` / `exact_replay_pending`: still to be retried

```
It has not finished: avibe.bot could not be reached or has not answered yet.
Nothing new was started. To try again:
  vibe remote rebind
```

中文说明：

```
还没有完成：暂时连不上 avibe.bot，或者它还没有答复。没有开始任何新的绑定。再试一次：vibe remote rebind
```

**H4u** · shortened for a rare state · `pairing_recovery_required` / `pairing_record_unusable`. Not called final, no promise to retry. Then D1 introduced by the last line, if `reset_interactively` admits it. How a person gets here: a setup started in the desktop app or with a pairing key was cut short, or the saved record no longer matches this computer's binding. (A record that cannot be read at all is C4.)

```
The record of that attempt cannot be used on this computer. It is not
continued or retried, and Avibe will not guess whether it completed.
Binding as a new computer clears that record.
```

中文说明：

```
那一次绑定的记录在这台电脑上已经用不了。它不会被继续或重试，Avibe 也不会猜它有没有完成。
作为一台新电脑重新绑定会清除这条记录。
```

**H4b** · `operation_in_progress`: another process holds it

```
Avibe is working on it right now. Nothing new was started. In a moment, run:
  vibe remote rebind
```

中文说明：

```
Avibe 正在处理它。没有开始任何新的操作。稍后运行：vibe remote rebind
```

**H5** · state `resetting` at start

```
A reset of this computer's binding was started and has not finished.
Finishing it now.
```

中文说明：

```
这台电脑的绑定重置已经开始，但还没有完成。现在把它做完。
```

**H6** · the step returned `unpaired`: D3, then F1 and the link

```
Done. This computer is no longer bound to any Avibe. Everything on it is as it was.
```

中文说明：

```
（同 D3）
```

**H7** · the step returned `resetting` (for example the tunnel has not stopped yet)

```
It could not finish yet. Remote access on this computer stays off until it
does. To try again:
  vibe remote rebind
```

中文说明：

```
暂时还完成不了。完成之前，这台电脑的远程访问保持关闭。再试一次：vibe remote rebind
```

**H8a** · shortened; reads as a reset, not a repair; D1's weight · `reset_repair_required` with no recoverable binding next to it, and `reset_interactively(repair=True)` accepts. These three lines, then all of D1. `{category}` is one of the four endings. How a person gets here: a reset was cut short (power loss, a killed process) and the binding or its files then changed, or the record was damaged on disk.

```
A reset of this computer was started earlier and cannot be continued:
{category}
What follows is a new reset of this computer as it is now.
its record on this computer is damaged.
this computer's binding changed after it was started.
this computer's binding files changed after it was started.
its record no longer matches this computer.
```

中文说明：

```
这台电脑之前开始过一次重置，它没法继续：{category}
接下来是对这台电脑现在的状态重新做一次重置。
四种原因：它在这台电脑上的记录已损坏／重置开始之后这台电脑的绑定变了／重置开始之后这台电脑的绑定文件变了／它的记录和这台电脑对不上。
```

**H8b** · shortened · `reset_repair_required` and the backend cannot verify what a new reset would clear Nothing is asked. How a person gets here: a reset was cut short and the data folder was then moved or restored from a backup, so the record points somewhere else.

```
A reset of this computer was started, and it can neither be finished nor
replaced by a new one safely from here. Nothing more was changed: remote
access stays off, and your data on this computer is not affected.
For help, see https://docs.avibe.bot/troubleshoot/common-issues or ask an
Agent in Avibe to report this to Avibe.
```

中文说明：

```
这台电脑开始过一次重置，在这里既不能把它做完，也不能安全地换成一次新的重置。没有再改动任何东西：远程访问保持关闭，这台电脑上的数据不受影响。
求助：https://docs.avibe.bot/troubleshoot/common-issues ，或者请 Avibe 里的 Agent 把这个问题报告给 Avibe。
```

**H9** · Product: decided November 22, this text · `reset_repair_required` with `next_action=confirm_resume_pairing`. Its own question, through `resume_interactively(confirm_pairing=..., expected_hostname=...)`; never the reset question. The domain line is the approved domain the backend returns and is handed back with the answer; when the backend cannot give one, the second variant of that line is printed. Never an account id. How a person gets here: a bind was approved and cut short before it was saved, and a reset record on the same computer is damaged.

```
Two unfinished things are on this computer:
  - a binding that was approved earlier and can still be completed
      Domain: {host}
      Its domain is shown when it has finished.
  - a reset that was started and cannot be continued: {category}

Answer yes to complete the binding and drop the reset. Remote access may
still be off afterwards; the command to turn it on is shown then.
Anything else leaves both as they are, with remote access off.
Complete the binding and drop the reset? [y/N] 
```

中文说明：

```
这台电脑上有两件没做完的事：
  - 之前确认过、现在仍然可以完成的一次绑定
      域名：{host}（读不到域名时改为：完成后会显示它的域名）
  - 一次已经开始、没法继续的重置：{category}
回答 y：完成那次绑定，并放弃那次重置。之后远程访问可能仍然是关着的，那时会显示打开它的命令。
其他任何回答：两件事都保持原样，远程访问保持关闭。
完成绑定并放弃重置？[y/N]
```

**H9a** · anything but `y` at H9

```
Nothing changed. Both are as they were, with remote access off.
```

中文说明：

```
什么都没有改变。两件事都保持原样，远程访问保持关闭。
```

**H9b** · `y` at H9 and the reset was withdrawn; H2, H4 or H4b follows by what the binding step returned

```
The reset was dropped.
```

中文说明：

```
那次重置已经放弃。
```

**H9c** · `y` at H9 but the computer changed while the question was open

```
This computer changed while you were answering, so nothing was done.
To look again:
  vibe remote rebind
```

中文说明：

```
在你回答期间这台电脑发生了变化，所以什么都没有做。再看一次：vibe remote rebind
```

**H9d** · reviewer T-4 · Ctrl+C after `y` at H9 with both records still there. If the reset was already dropped, H9b and then H2 or H4 by the state read again.

```
Stopped. Nothing was done: both are still there, with remote access off.
To look again:
  vibe remote rebind
```

中文说明：

```
已停止。什么都没有做：两件事都还在，远程访问保持关闭。再看一次：vibe remote rebind
```

## I. Other `vibe remote` commands (English, also read by Agents)

| Where | State / code | Text |
|---|---|---|
| `vibe remote status`, new line (and `binding_state` in `--json`) | `binding_rejected` | `  Binding: refused by avibe.bot. See: vibe remote rebind` |
| | `pairing_recovery_required` | `  Binding: an earlier attempt is unfinished. Continue it: vibe remote rebind` |
| | `resetting` | `  Binding: a reset is unfinished. Continue it: vibe remote rebind` |
| | `reset_repair_required` | `  Binding: a reset cannot finish. See: vibe remote rebind` |
| `vibe remote start` | `binding_rejected` | `Remote access was not started: avibe.bot is refusing this computer's binding.` + `See what can be done: vibe remote rebind` |
| | `reset_in_progress` | `Remote access was not started: a reset of this computer's binding has not finished.` + `Continue it: vibe remote rebind` |
| `vibe remote pair`, success on a computer whose remote access was off | pairing ok, no `start` in the result | `Pairing saved. Remote access was off on this computer and stays off.` + `Turn it on with:` + `  vibe remote start` (exit 0) |
| `vibe remote pair`, success on a computer where remote access is disabled in settings | pairing ok, no `start`, `enabled` false | `Pairing saved. Remote access is disabled in this computer's settings and stays disabled.` + `vibe remote start does not turn it on. In the settings file, set` + `remote_access.vibe_cloud.enabled to true:` + `  {path of the settings file}` + `then run:` + `  vibe remote start` (exit 0) |
| `vibe remote pair`, the pairing tried a start and the setting refused it | pairing ok, `start.error` `remote_access_disabled`, `enabled` false | the same sentence and five lines as the row above (exit 0); one state has one wording |
| `vibe remote start` on a bound computer with nothing unfinished | `remote_access_disabled`, binding state `bound` | `Remote access was not started: it is disabled in this computer's settings.` + the same five lines |
| `vibe remote start`, the setting disabled **and** binding work in the way | `remote_access_disabled`, binding state `resetting` or `reset_repair_required` | `Remote access was not started: a reset of this computer's binding has not finished.` + `Continue it: vibe remote rebind`. Never the settings lines: the setting is part of what that work compares. |
| | binding state `pairing_recovery_required` | `Remote access was not started: an earlier attempt to bind this computer has not finished.` + `Continue it: vibe remote rebind` |
| | binding state `binding_rejected` | `Remote access was not started: avibe.bot is refusing this computer's binding.` + `See what can be done: vibe remote rebind` |
| | any other binding state (record damaged, unknown) | `Remote access was not started.` + `See what can be done: vibe remote rebind` |
| `vibe remote pair` | `operation_in_progress` | `Another change to this computer's binding or domain is running. No pairing key was used.` + `When it has finished, run the command again.` |
| `vibe remote` on a bound computer, last lines | already bound | `To bind this computer again, or to another account:` + `  vibe remote rebind` + `To change the domain, open Devices in Avibe.` |
| a settings save that would change the binding (`vibe/api.py`, shown by the settings page as the error) | pairing identity changed in a generic save | `This computer's binding is not changed by saving settings. Bind it again from Devices, or run 'vibe remote rebind' on this computer.` (was: `Pairing identity must be changed through the remote access pairing flow`) |

## J. Command help

`vibe remote rebind --help` and the changed lines of `vibe remote pair --help`
are in `vibe/cli.py` (`_remote_rebind_help_text`, `_remote_pair_examples_text`).
They carry the same sentences as C2, D1 and F1 in short form: the names say 30
days, the browser way carries Product's sentence, and the pair help says that
pairing over a bound computer replaces the binding, signs every remote device
out, and leaves remote access off where it was off.

## What was decided, and what is open

- **English only at the terminal**, written in code; no catalog entries, no
  `--lang` (Product).
- **C2, D1, E3, F1, G4** decided by Product on November 21; **G1** and **B6**'s third sentence on November 22. D1 ends with the
  "also delete the old Avibe" sentence; C2 keeps the original one. Names say 30
  days and never mention the margin.
- **G2**: the offer of a new computer after the loopback refusal stays, as a
  separate choice that defaults to no. **G8**: an unexplained refusal infers
  nothing and offers no reset (reviewer).
- **H9** decided by Product on November 22: asked without the email, with the
  approved domain when the backend can give it.
- **B6**: a request avibe.bot refused for good is said once under the state
  lines, not as a dialogue (lead).
- **Rare states carry little text** (Product's limit): H4u, H8a and H8b are
  shortened, and each rare state names how a person gets there. No further
  question is added to this command without that line.
- **H8b's help pointer** is the public troubleshooting page plus "ask an Agent
  in Avibe to report this to Avibe", the repository's existing feedback route.
