# usb_udev_namer

幫 USB 序列裝置(`ttyUSB*`、`ttyACM*`)取一個固定名稱的工具,例如 `/dev/usb_imu`。

Linux 給 USB 序列裝置的 `/dev/ttyUSB0`、`/dev/ttyUSB1` 取決於插入順序,一換孔、一重插就可能對調。
正規解法是寫 udev 規則建 symlink;本工具用互動選單幫你寫好、套用並當場驗證,不用懂 udev 語法:

插上裝置 → 從清單選它 → 輸入名稱 → 畫面直接顯示 `/dev/<名稱> -> ttyUSB0`

## 需求

- Linux 或 WSL2(macOS 沒有 udev,不適用;直接用 `/dev/cu.usbserial*`)
- Python 3.8 以上(只用標準函式庫,不需 `pip install`)
- 有 `sudo` 權限(寫入 `/etc/udev/rules.d/` 需要)
- WSL2:裝置要先用 [wsl_usb_auto](https://github.com/alianlbj23/wsl_usb_auto) 掛進 WSL,本工具才看得到

## 安裝

把 `usb_udev_namer.py` 放到任意位置即可,不需額外安裝。

## 使用方式

### 互動選單(最簡單)

不加參數直接執行就會出現選單:

```bash
python3 usb_udev_namer.py
```

```
========== USB 裝置固定名稱 (udev) ==========
目前名稱:1 個(usb_imu -> ttyUSB0)
快捷指令:usbname
  1. 幫裝置取名稱
  2. 查看所有名稱(含健康檢查)
  3. 刪除名稱
  4. 重新套用規則(重插後名稱沒出現時用)
  5. 查看目前插著的 USB 序列裝置
  6. 設定快捷指令(之後打 usbname 就能開選單)
  7. 移除快捷指令
  8. 全部清除(移除規則檔與所有設定)
  0. 離開
請選擇 [0-8]:
```

選「1. 幫裝置取名稱」的流程:

```
目前插著的 USB 序列裝置:
  1. /dev/ttyUSB0  10c4:ea60  Silicon Labs CP2102 USB to UART Bridge Controller  序號 0001  埠 1-3.2
請選擇裝置 [1-1](0 返回):1
輸入名稱(之後會出現在 /dev/ 底下,例如 usb_imu;直接 Enter 取消):usb_imu

辨識方式:VID:PID 10c4:ea60 + 序號 0001。插哪個 USB 孔都可以。
要改用實體 USB 埠辨識嗎?(換孔就要重新取名)[y/N]
將寫入 /etc/udev/rules.d/99-usb-udev-namer.rules(需要 sudo)。繼續?[y/N] y
[sudo] password for user:
套用中...
lrwxrwxrwx 1 root root 7 Nov 11 10:00 /dev/usb_imu -> ttyUSB0
```

選單裡的「設定快捷指令」會在 `.bashrc` 加一個 alias(預設 `usbname`),之後打 `usbname` 就能開啟選單。

### 指令模式(給腳本用;選單都做得到,不用記)

| 指令 | 說明 |
|------|------|
| `python3 usb_udev_namer.py scan` | 列出目前插著的 USB 序列裝置 |
| `python3 usb_udev_namer.py add /dev/ttyUSB0 -n usb_imu` | 幫裝置取名稱,可加 `--by auto\|serial\|port` 強制辨識方式 |
| `python3 usb_udev_namer.py list` | 列出所有名稱與健康檢查 |
| `python3 usb_udev_namer.py remove usb_imu` | 刪除名稱 |
| `python3 usb_udev_namer.py apply` | 依紀錄重新產生並套用規則 |
| `python3 usb_udev_namer.py uninstall` | 刪除規則檔、所有設定與 `.bashrc` 區塊(會確認兩次) |

`add` 在名稱沒有出現時會以結束代碼 1 結束,方便腳本判斷。

## 裝置辨識方式

電腦能區分兩個 USB 裝置的資訊只有三樣:VID:PID、序號、實體 USB 埠。工具會自動選擇:

| 情況 | 用什麼辨識 | 使用上 |
|------|-----------|--------|
| 有序號,且「VID:PID + 序號」在目前插著的裝置中唯一 | VID:PID + 序號 | 插哪個孔都可以 |
| 沒有序號,或和另一個插著的裝置完全相同 | 實體 USB 埠 + VID:PID | 要固定插同一個孔,換孔要重新取名 |

所以**同款裝置有兩個以上時,取名時請全部插著**,工具才看得出它們序號相同、自動改用實體埠。
不提供「只用 VID:PID」:之後插入同款晶片時會悄悄撞名。

## 運作方式

1. 名稱紀錄在 `~/.config/usb-udev-namer/rules.json`(唯一真實來源)。
2. 每次變更後,整個重新產生 `/etc/udev/rules.d/99-usb-udev-namer.rules`,例如:

   ```
   SUBSYSTEM=="tty", ATTRS{idVendor}=="10c4", ATTRS{idProduct}=="ea60", ATTRS{serial}=="0001", SYMLINK+="usb_imu", MODE="0666"
   SUBSYSTEM=="tty", KERNELS=="1-3.4", ATTRS{idVendor}=="10c4", ATTRS{idProduct}=="ea60", SYMLINK+="usb_esp32", MODE="0666"
   ```

3. 套用:`sudo cp` → `sudo udevadm control --reload-rules` → `sudo udevadm trigger --subsystem-match=tty --action=add`,
   最多等 3 秒看 `/dev/<名稱>` 出現並印出 `ls -l`。sudo 或 udevadm 失敗時 `rules.json` 不會更新。
4. `MODE="0666"` 讓所有使用者都能讀寫,省去加入 dialout 群組與重新登入;要收緊權限改程式裡的 `MODE` 常數。
5. 本工具只動自己產生的那個規則檔,不碰 `/etc/udev/rules.d/` 裡的其他檔案。

## 相關檔案

| 路徑 | 用途 |
|------|------|
| `~/.config/usb-udev-namer/rules.json` | 名稱紀錄 |
| `/etc/udev/rules.d/99-usb-udev-namer.rules` | 由工具產生的 udev 規則(請勿手改) |
| `~/.bashrc` | 快捷指令 alias(以 `# >>> usb-udev-namer >>>` 標記包起來,`uninstall` 可乾淨移除) |

## 常見問題

- **名稱沒有出現**:選「4. 重新套用規則」。若仍沒有,選「2. 查看所有名稱」看健康檢查。
- **WSL 上名稱一直不出現**:WSL 預設可能沒有啟動 udev 服務,執行 `sudo service udev restart` 後再選 4。
  長期解法是在 `/etc/wsl.conf` 加入:

  ```ini
  [boot]
  systemd=true
  ```

  再於 Windows 執行 `wsl --shutdown` 重開 WSL。
- **健康檢查顯示「符合 2 個裝置」**:兩個裝置的 VID:PID 與序號完全相同(取名時只插了一個)。
  選 3 刪除該名稱,把兩個裝置都插上再重新取名,工具會自動改用實體埠。
- **換了 USB 孔名稱就不見**:該名稱是用實體埠辨識的,請插回原本的孔,或重新取名。
- **sudo 驗證失敗**:密碼錯誤或帳號沒有 sudo 權限;此時不會做任何變更。

## 測試

```bash
python3 -m pytest tests -q
python3 -m flake8 usb_udev_namer.py tests
```

測試不需要 root,也不需要真實裝置:sysfs 用暫存目錄模擬,外部指令以假物件取代。

## 授權

Apache-2.0
# usb_udev_namer
