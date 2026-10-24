# lab

一个用于孵化项目的容器仓库（container repo）。

具体项目以独立子目录的形式加入，每个子目录对应一个完整项目，自带 README 与运行说明。

## 约定

- 每个项目一个顶层目录，目录名即项目名
- 各项目自带 README，说明用途、运行方式与依赖
- 互不依赖的项目不共享依赖与配置
- 凭据、密钥、`.env` 一律不入库（见根目录 `.gitignore`）

## 项目列表

| 项目 | 目录 | 说明 |
| --- | --- | --- |
| mcd-app | [`projects/mcd-app/`](projects/mcd-app/) | 麦当劳点餐助手原型：选门店、浏览真实菜单、定制餐品、人均/总额预算、订单预览 |

### 快速预览（无需任何凭据）

```bash
git clone https://github.com/imechoq/lab.git
cd lab/projects/mcd-app
python3 -m http.server 8777
# 打开 http://127.0.0.1:8777/app/
```

各项目的完整运行与配置步骤见其目录下的 README。
