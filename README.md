# DWG 图纸搜索工具

> **批量搜索 / 替换 DWG 图纸文件名与文字内容的桌面工具**
> 基于 `accoreconsole` 无界面引擎读取图纸内容，本地建立索引，支持按文件名、正文内容关键词（含正则表达式）快速检索。

![Windows](https://img.shields.io/badge/Platform-Windows%2010%2F11-blue)
![Version](https://img.shields.io/badge/Version-2.19.0-orange)
![License](https://img.shields.io/badge/License-Proprietary-red)
![Download](https://img.shields.io/github/downloads/qqhsx/dwg-search/total?label=Downloads)

---

## 📥 下载

| 版本 | 类型 | 适用场景 | SHA256 |
|------|------|----------|--------|
| **[v2.19.0 安装版](https://github.com/qqhsx/dwg-search/releases/download/v2.19.0/DWG_Search_Setup_x64_2.19.0.zip)** | `.zip` (解压即用) | 首次安装、解压运行、可手动创建快捷方式 | `见 Release 页面` |
| **[v2.19.0 便携版](https://github.com/qqhsx/dwg-search/releases/download/v2.19.0/DWG_Search_Portable_x64.zip)** | `.zip` | 免安装、绿色运行、U 盘携带、多版本共存 | `见 Release 页面` |

> **⚡ 提示**：便携版解压即用，无需管理员权限；安装版会注册右键菜单「用 DWG 图纸搜索工具搜索此目录」。

---

## ✨ 核心功能

| 功能 | 说明 |
|------|------|
| **全文检索** | 索引 DWG/DXF 图纸内的**文字内容**（模型空间、图纸空间、块定义、属性块） |
| **文件名搜索** | 支持通配符 `*` `?`、正则表达式、大小写敏感切换 |
| **组合筛选** | 扩展名、修改日期范围、文件大小范围、图纸版本 |
| **批量替换** | 支持正则替换、预览差异、撤销恢复、替换日志导出 |
| **右键菜单集成** | 资源管理器右键文件夹 → 「用 DWG 图纸搜索工具搜索此目录」 |
| **书签收藏** | 一键保存常用「文件名+内容」搜索条件 |
| **多窗口** | `Ctrl+Shift+N` 新建独立窗口，共享同一后台索引 |
| **系统托盘** | 关闭窗口最小化到托盘，后台索引继续运行 |
| **开机自启** | 可选开机后台启动，索引自动更新 |
| **数据备份/恢复** | 索引数据库一键导出/导入，迁移无忧 |

---

## 🖥️ 系统要求

| 组件 | 要求 |
|------|------|
| **操作系统** | Windows 10 / 11 (x64) |
| **AutoCAD** | **可选** — 安装 AutoCAD 2018~2025 可启用 `accoreconsole` 高精度提取引擎；<br>未装 AutoCAD 时自动回退到纯 .NET `ACadSharp` 引擎（无需安装 CAD） |
| **.NET Runtime** | 内置 `DwgTextReplacer.exe` 需 .NET Framework 4.8（Win10/11 预装） |
| **磁盘空间** | 索引约占原图纸总大小 5%~15%（视文字密度而定） |
| **内存** | 建议 4 GB+；大量图纸并发索引时 8 GB+ 更流畅 |

> **无 AutoCAD 也能用** — 纯 .NET 引擎覆盖 90%+ 常见图纸格式，仅极少数复杂代理图形/自定义对象需 AutoCAD 引擎。

---

## 🚀 快速开始

### 便携版（推荐新用户）
```powershell
# 1. 下载 DWG_Search_Portable_x64.zip
# 2. 解压到任意文件夹（如 D:\Tools\DWG_Search）
# 3. 双击 DWG_Search.exe 运行
# 4. 点击「添加搜索目录」选择图纸文件夹 → 「开始索引」 → 等待完成即可搜索
```

### 安装版
```powershell
# 1. 下载 DWG_Search_Setup_x64.exe
# 2. 以管理员身份运行安装向导
# 3. 完成后，资源管理器右键任意文件夹即可看到「用 DWG 图纸搜索工具搜索此目录」
```

---

## 📖 使用指南

### 建立索引
1. 点击工具栏 **「添加搜索目录」**（或 `Ctrl+D`）
2. 选择包含 DWG 图纸的根文件夹（支持多选、支持网络路径）
3. 点击 **「开始索引」**（或 `F5`）— 首次全量索引较慢，后续仅增量更新
4. 状态栏显示「索引完成」后即可搜索

### 搜索技巧
| 场景 | 操作 |
|------|------|
| 精确匹配短语 | 直接输入关键词（默认不区分大小写） |
| 正则表达式 | 勾选「正则」或按 `Alt+R` 切换，如 `^[A-Z]{3}-\d{4}$` |
| 仅搜文件名 | 在「文件名」框输入，留空「内容」框 |
| 仅搜内容 | 留空「文件名」框，在「内容」框输入 |
| 组合搜索 | 两框都填，自动「与」逻辑 |
| 筛选扩展名 | 下拉选择 `.dwg` / `.dxf` / `全部` |
| 日期范围 | 点击日期输入框选择「修改时间」区间 |

### 批量替换
1. 搜索出目标图纸列表
2. 选中需替换的行（`Ctrl+A` 全选、`Shift` 连选、`Ctrl` 单选）
3. 点击工具栏 **「文字替换」**（或 `Ctrl+H`）
4. 输入「查找内容」「替换为」，可勾选「正则」「区分大小写」「仅模型空间」等
5. 点击 **「预览」** 确认差异 → **「执行替换」**
6. 替换完成后自动生成 `replace_log_YYYYMMDD_HHMMSS.csv` 可追溯

---

## 🔒 许可证与免责声明

### 专有软件 / 不开源
**本软件仅提供二进制分发，源代码不公开。**

- ✅ 允许：免费下载、个人/商业用途使用、通过原始 Release 页面链接分享
- ❌ 禁止：反编译、逆向工程、修改二进制、提取核心组件单独使用、去除版权/水印、重新打包分发
- ⚠️ 无担保：按「现状」提供，作者不承担任何直接/间接损失责任
- 📄 完整条款见 [LICENSE](LICENSE) / Release 页面「License」栏

> **为什么不开源？**  
> 核心索引/替换逻辑包含商业算法与 AutoCAD 交互细节，开源会引来大量「白嫖改名重打包」和恶意篡改收款码的行为。保持闭源能保证下载到的程序是原作者原版、未被植入后门。

---

## 🛡️ 安全与完整性验证

每个 Release 附带 `SHA256SUMS.txt`，下载后请验证：

```powershell
# PowerShell 验证
Get-FileHash -Algorithm SHA256 DWG_Search_Setup_x64.exe
# 对比 Release 页面的 SHA256SUMS.txt 内容
```

- 代码签名：暂无 EV 代码签名证书，Windows SmartScreen 可能提示「未识别的应用」— 点击「更多信息」→「仍要运行」
- 杀毒误报：PyInstaller 打包的单文件/单文件夹模式极易被启发式查杀，属误报。可上传 [VirusTotal](https://www.virustotal.com/) 多引擎查杀确认

---

## 🐛 问题反馈与建议

| 渠道 | 说明 |
|------|------|
| **GitHub Issues** | [提交 Bug / 功能建议](https://github.com/qqhsx/dwg-search/issues) — 请附上：版本号、操作系统、复现步骤、错误截图/日志 |
| **邮箱** | `qqhsx@qq.com`（仅限无法公开的安全/隐私问题） |

**常见问题自查**：
- 索引卡住/极慢 → 检查「排除目录」是否误包含大量无关文件、降低「内容提取并发数」
- 搜不到内容 → 确认图纸真有文字（非纯图形/光栅图）、尝试重建索引、切换提取引擎
- 替换后打不开 → 原图纸可能已损坏或版本不兼容，替换前自动备份 `.bak` 文件

---

## 🙏 致谢

- [ACadSharp](https://github.com/ACadSharp/ACadSharp) — 纯 .NET DWG/DXF 读写库
- [PyInstaller](https://pyinstaller.org/) — Python 打包工具
- [PyQt5](https://www.riverbankcomputing.com/software/pyqt/) — GUI 框架
- [Inno Setup](https://jrsoftware.org/isinfo.php) — 安装包制作

---

## 📞 联系作者

- **GitHub**: [@qqhsx](https://github.com/qqhsx)
- **Email**: qqhsx@qq.com
- **主页**: https://github.com/qqhsx/dwg-search

---

> **如果这个工具帮你省了时间，欢迎在「帮助 → 捐赠作者」里请作者喝杯咖啡 —— 完全自愿，不给也完全不影响使用。**