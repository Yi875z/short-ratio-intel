# short-ratio-intel

空売り比率インテリジェンス・システム
NEO真金融グランドマスター × Gemini 3 Flash 統合分析エンジン

## セットアップ

```powershell
cd C:\CarSol\short-ratio-intel
.\setup.ps1
```

## ナレッジファイルの配置

```
src\knowledge\files\ に以下の4ファイルをコピー:
  01_global_macro.md   ← Global_Macro_Dynamics.md から
  02_jpx_micro.md      ← JPX_Micro_Flows.md から
  03_options_gex.md    ← Options_and_GEX_Master.md から
  04_quant_psych.md    ← Quant_Tech_Psychology.md から
```

## 起動

```powershell
.venv\Scripts\Activate.ps1
streamlit run app\streamlit_app.py
```

## 開発計画書

docs\development_plan.md を参照

## 運用マニュアル

起動方法、日常運用、停止方法、トラブル対応、Web公開までの道筋は以下を参照してください。

```
docs\operation_manual.md
```
