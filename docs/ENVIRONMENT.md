# 実行環境と再現手順

## 使用する環境

| 項目 | 内容 |
|---|---|
| ホスト側の操作 | Windows PowerShell とブラウザー |
| Docker イメージ | `airobotbook/ros2-desktop-ai-robot-book-humble:latest` |
| コンテナ名の例 | `pai_ros2` |
| コンテナの基盤 | Ubuntu 22.04 / ROS 2 Humble |
| RMW | `rmw_cyclonedds_cpp` |
| シミュレーター | Gazebo Classic / `turtlebot3_gazebo` |
| ロボット・ワールド | `happy_mini` / `turtlebot3_world` |
| ナビゲーション | インストール済み Humble の標準 `nav2_params.yaml` と保存地図 |
| Python | コンテナ内の `/usr/bin/python3` |
| LLM | Gemini API の `gemini-3.5-flash-lite` |

`latest` タグの内容は将来更新され得る。依存環境を厳密に固定する場合は、実行時のイメージ ID / digest も記録する。

## 1. コンテナを用意する

**既に `pai_ros2` を使っている場合は、そのコンテナを再利用する。** 以下の確認は Windows PowerShell で行う。

```powershell
docker ps -a --filter "name=pai_ros2"
```

既存のコンテナが停止中なら起動する。

```powershell
docker start pai_ros2
```

以下の新規作成は、`pai_ros2` が存在しない場合だけ実行する。既存コンテナを削除・作り直す必要はない。

```powershell
$paiWorkspace = Join-Path $env:USERPROFILE "pai-final"
New-Item -ItemType Directory -Force -Path $paiWorkspace | Out-Null
docker run -d --name pai_ros2 -p 127.0.0.1:6080:80 --shm-size=1g --mount "type=bind,source=$paiWorkspace,target=/workspace" airobotbook/ros2-desktop-ai-robot-book-humble:latest
```

[http://localhost:6080](http://localhost:6080) を開く。VNC パスワードを求められたら `ubuntu` を入力する。Ubuntu デスクトップの **Terminator** を開き、以降の Linux コマンドを実行する。

## 2. リポジトリーをコンテナに配置する

以下は `/workspace/pai_gemini_v1` に配置する例だ。別の場所へ clone / 展開した場合は、以降の `cd` をそのリポジトリールートに合わせる。

### 配布 ZIP を使う場合

Windows の Downloads に保存した `pai_gemini_v1.zip` を、PowerShell からコピー・展開し、配置先の所有者を `ubuntu` にする。

```powershell
docker cp "$env:USERPROFILE\Downloads\pai_gemini_v1.zip" pai_ros2:/workspace/pai_gemini_v1.zip
docker exec -u root pai_ros2 /usr/bin/python3 -m zipfile -e /workspace/pai_gemini_v1.zip /workspace
docker exec -u root pai_ros2 chown -R ubuntu:ubuntu /workspace/pai_gemini_v1
```

所有者を設定するのは、Terminator の `ubuntu` ユーザーがリポジトリー内に `./logs` を作成できるようにするためだ。対象は `/workspace/pai_gemini_v1` ディレクトリーだけとする。

配置後、コンテナ内の Terminator でリポジトリールートへ移動する。

```bash
cd /workspace/pai_gemini_v1
```

### GitHub から取得した場合

GitHub の公開リポジトリーを clone、または ZIP で取得する。Windows 側で展開した場合は、`nav_gemini.py` が直下にあるフォルダーを `pai_gemini_v1` として Downloads に置けば、次の PowerShell コマンドでコピーできる。

```powershell
docker cp "$env:USERPROFILE\Downloads\pai_gemini_v1" pai_ros2:/workspace/
docker exec -u root pai_ros2 chown -R ubuntu:ubuntu /workspace/pai_gemini_v1
```

既にコンテナ内に取得してある場合は、そのリポジトリールートを使用する。

## 3. ROS ターミナルの共通準備

Gazebo、Nav2、移動 CLI はそれぞれ別の Terminator で実行する。**新しいターミナルごとに次のブロックを実行する。**

```bash
source /opt/ros/humble/setup.bash
source /home/ubuntu/airobot_ws/install/setup.bash
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export TURTLEBOT3_MODEL=happy_mini
cd /workspace/pai_gemini_v1
```

計画確認だけの `--plan-only` では ROS の準備は不要だ。走行では既存ノードと同じ RMW を使う。

## 4. Gazebo を起動する

ターミナル 1 で共通準備を済ませてから起動する。

```bash
source /usr/share/gazebo/setup.sh
ros2 launch turtlebot3_gazebo turtlebot3_world.launch.py
```

既に同じワールドが動いている場合は、その Gazebo を利用する。別の `empty_world` などが起動中なら、その起動ターミナルを `Ctrl+C` で終了してからワールドを切り替える。

## 5. 保存地図で Nav2 を起動する

Cartographer が動作中なら、その起動ターミナルで終了する。保存地図を使う AMCL と、SLAM による地図・自己位置の配信を同時に動かさない。

ターミナル 2 で共通準備を済ませ、**リポジトリールートにいる状態で**次を実行する。

```bash
ros2 launch turtlebot3_navigation2 navigation2.launch.py \
  use_sim_time:=true \
  map:="$(pwd)/maps/tb3_world.yaml" \
  params_file:=/opt/ros/humble/share/nav2_bringup/params/nav2_params.yaml
```

`$(pwd)` により地図を絶対パスで渡す。地図 YAML と PGM は同じ `maps` ディレクトリーに置く。配布モデルの既定パラメーターではなく、インストール済み Humble の標準パラメーターを明示する構成で動作を確認している。

既に同じ地図・パラメーターで Nav2 が動いている場合は、その起動状態を利用する。

### RViz の初期姿勢

1. 保存地図が表示されたら、上部の **`2D Pose Estimate`** を押す。
2. Gazebo 内のロボットの現在位置に相当する地図上の場所をクリックし、向いている方向へドラッグする。
3. レーザーの点が地図の壁・柱とおおむね重なっていることを確認する。
4. `Navigation 2` パネルの `Navigation` と `Localization` が `active` になっていることを確認する。

地図上の登録地点は [地図と A / B の図](map_waypoints.png) を参照できる。Gazebo の world 座標と地図の `map` 座標は、原点や向きが一致するとは限らない。

初期姿勢をまだ指定していない間に、RViz で `queue is full` などの TF 待ちが表示されることがある。まず初期姿勢を指定し、レーザーと地図の整合を確認する。Nav2 は既定で自動起動するため、通常 `Startup` ボタンの操作は不要だ。

既に位置推定と手動ゴール指定が動いている場合は、その状態から CLI へ進める。

## 6. CLI を実行する

ターミナル 3 で共通準備を済ませる。API キーの入力手順は [README](../README.md#2-api-キーを設定する) を参照する。`read` の行は単独実行し、キー入力後に別途 `export` する。

```bash
/usr/bin/python3 -u nav_gemini.py --poses-dir ./poses --log-file ./logs/gemini_nav.jsonl \
  '受付、倉庫、受付の順に回って'
```

相対パスを使っているので、カレントディレクトリーはリポジトリールートにする。コードの既定値 `/workspace/poses`、`/workspace/logs` は変更していないため、この再現手順ではオプションを明示する。

キーボード操縦を使っていた場合は `s` で停止し、`Ctrl+C` で操縦プログラムを終了してから CLI を実行する。CLI の走行中は別の CLI や RViz から移動目標を送らない。

## 7. 起動で困った場合

| 症状 | 最初に確認すること |
|---|---|
| `ros2: command not found` | 正しい講義コンテナか、ROS の 2 本の `setup.bash` を読み込んだか |
| GUI が表示されない | `http://localhost:6080` の Ubuntu Terminator から起動しているか |
| 地図を読み込めない | リポジトリールートにいるか、`maps/tb3_world.yaml` と `.pgm` が両方あるか |
| AMCL が見つからない、ノード同士が通信しない | 各ターミナルで `RMW_IMPLEMENTATION=rmw_cyclonedds_cpp` を設定したか、Nav2 の起動ログにエラーがないか |
| レーザーの点が大きくずれる | `2D Pose Estimate` の位置と向きが Gazebo の現在状態に対応しているか |
| 初期姿勢指定後も TF 待ちが続く | Gazebo が再生中か、Nav2 の `use_sim_time:=true` を指定したか、同じワールドを使っているか |

本手順は利用者の講義コンテナでの動作結果と配布元の構成を基に整理した。新しいホストでのコンテナ作成からの通し検証は未実施だ。

## 配布元

- [講義 Docker イメージ](https://github.com/AI-Robot-Book-Humble/docker-ros2-desktop-ai-robot-book-humble)
- [Happy Mini / TurtleBot3](https://github.com/AI-Robot-Book-Humble/turtlebot3_happy_mini)
- [TurtleBot3 の Navigation Simulation](https://emanual.robotis.com/docs/en/platform/turtlebot3/nav_simulation/)
