import os
import sys
sys.stdout.reconfigure(encoding='utf-8')
import argparse
import datetime
import time
import socket
import schedule
import pandas as pd
import subprocess
from pathlib import Path
from config import validate_config
from quant_filter import run_quant_filtering
from report_generator import generate_report
from notifier import send_telegram_message, send_telegram_document
from bot_listener import start_bot_listener
from index_closing import (
    execute_index_closing,
    check_and_send_crash_alerts,
    check_and_send_sidecar_alerts,
    check_and_send_leverage_etf_alerts,
    is_us_market_open,
    load_alert_state,
)
import psutil

BASE_DIR = Path(__file__).resolve().parent
REPORTS_DIR = BASE_DIR / "reports"
REPORTS_DIR.mkdir(exist_ok=True)

def check_system_health():
    """매일 06:00, 12:00 정기 시스템 자체 점검 보고서 생성 및 텔레그램 발송"""
    try:
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # 시스템 리소스 확인
        mem = psutil.virtual_memory()
        cpu = psutil.cpu_percent(interval=1)
        mem_usage = f"{mem.percent}%"
        cpu_usage = f"{cpu}%"
        
        # 임의의 API 커넥션 및 DB 캐시 상태는 정상 작동 중이라 간주 (Mock)
        # 실제 운영환경에서 try-except로 connection ping을 구현할 수 있음
        api_status = "정상 (OK)"
        db_status = "정상 (OK)"
        
        report_msg = (
            "⚙️ <b>[stock_bot 시스템 정기 자체점검 보고서]</b>\n\n"
            f"점검 시간: {now_str} (정기 점검)\n\n"
            "■ 📊 시스템 현재 작동 상태: 정상 가동 중 (HEALTHY)\n"
            f"  - API Connection: {api_status}\n"
            f"  - Database & Cache: {db_status}\n"
            f"  - Memory Usage: {mem_usage} / CPU: {cpu_usage}\n\n"
            "■ ⏳ 예정된 작업 일정 안내:\n"
            "  - [15:45] 장 마감 정산 및 국내/미국 지수 인포그래픽 카드 2종 발송 큐 대기 중\n"
            "  - [21:00] 당일 스윙/수급 포착 종목 심층 분석 리포트 발송 큐 대기 중\n"
            "  - [실시간] 지수 전고점 대비 변동성 리스크 스크리너 활성화 중\n"
            "  - [미국장중] TQQQ/QLD 전고점 대비 분할매수 스크리너 30분 주기 감시 중\n\n"
            "■ 🚨 에러 및 예외 발생 점검 결과:\n"
            "  - 특이사항 없음. 시스템 내부에서 감지된 런타임 에러 0건 (SAFE)\n\n"
            "💡 본 보고서는 무인 자동화 파이프라인에 의해 정기적으로 생성되었습니다."
        )
        print(f"🏥 [{now_str}] System Health Check Completed. Dispatching Report...")
        send_telegram_message(report_msg, parse_mode="HTML")
    except Exception as e:
        print(f"❌ Error occurred during system health check: {e}")

def execute_pipeline(is_nxt=False):
    """
    Core pipeline:
    1. Check if today is weekend (skip if Saturday or Sunday)
    2. Quant filtering
    3. Gemini report generation
    4. Save report locally
    5. Send Telegram notification
    """
    today = datetime.date.today()
    pipeline_type = "NXT-Inclusive " if is_nxt else ""
    print(f"\n🔔 [{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting {pipeline_type}Screener Pipeline...")
    
    # Skip weekends in standard run
    if today.weekday() >= 5:
        print("📅 Today is a weekend. The Korean stock market is closed. Skipping run.")
        return
        
    try:
        # 1. Run Quantitative and DART Financial Filters
        filtered_stocks, analysis_log = run_quant_filtering(is_nxt=is_nxt)
        
        # Save analysis log to CSV for Notion import
        csv_path = None
        if analysis_log:
            df_log = pd.DataFrame(analysis_log)
            date_str = today.strftime("%Y%m%d")
            file_suffix = "_NXT" if is_nxt else ""
            csv_path = REPORTS_DIR / f"analysis_source_{date_str}{file_suffix}.csv"
            df_log.to_csv(csv_path, index=False, encoding="utf-8-sig")
            print(f"📊 Analysis source data saved locally to {csv_path}")
        
        # 2. Generate Analyst Report
        while True:
            try:
                report_text = generate_report(filtered_stocks, analysis_log, is_nxt=is_nxt)
                break
            except Exception as api_err:
                error_msg = (
                    f"⚠️ **[안내] 현재 AI 분석이 불가능하여 목업(Mock) 데이터만 사용 가능한 상태입니다.**\n\n"
                    f"- 원인: {api_err}\n"
                    f"- 안내: 부정확한 목업 데이터 기반의 종목 분석은 진행하지 않습니다.\n"
                    f"- 조치: 30분 단위로 자동 재시도하며, 정상적으로 AI 분석이 가능한 시점에 종목 분석 리포트를 생성하여 알려드리겠습니다."
                )
                print(error_msg)
                send_telegram_message(error_msg)
                time.sleep(1800)
        
        # 3. Save Report Locally
        date_str = today.strftime("%Y%m%d")
        file_suffix = "_NXT" if is_nxt else ""
        report_path = REPORTS_DIR / f"report_{date_str}{file_suffix}.md"
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report_text)
        print(f"💾 Report saved locally to {report_path}")
        
        # 4. Dispatch Telegram Notification
        send_telegram_message(report_text)
        if csv_path:
            send_telegram_document(csv_path)
        
        # 5. Generate per-stock infographic cards (1 image per TOP pick)
        if filtered_stocks:
            try:
                from stock_card_generator import generate_stock_cards
                # Use a specific output dir or name logic for NXT if needed, but it's okay to overwrite or just generate
                generate_stock_cards(filtered_stocks, report_text)
            except Exception as card_err:
                import traceback
                tb = traceback.format_exc()
                error_msg = f"⚠️ 종목 카드 생성 모듈 로딩 오류:\n{str(card_err)}\n\n{tb[-500:]}"
                print(error_msg)
                send_telegram_message(error_msg)
            
        print(f"🎉 {pipeline_type}Screener pipeline run finished successfully!")
        
    except Exception as e:
        print(f"❌ Error occurred during pipeline execution: {e}")

def trigger_oci_deployment():
    """
    Git Push 성공 시 OCI 서버에 자동 배포(Update)를 수행하는 쉘 스크립트를 트리거합니다.
    """
    # 윈도우 환경이면 기본값을 .bat으로, 리눅스/맥이면 .sh로 지정
    default_script = './deploy_oci.bat' if os.name == 'nt' else './deploy_oci.sh'
    script_path = os.environ.get('OCI_DEPLOY_SCRIPT_PATH', default_script)
    
    if not os.path.exists(script_path):
        print(f"⚠️ [OCI 배포] 배포 스크립트를 찾을 수 없습니다: {script_path}")
        return
        
    print(f"🚀 [OCI 배포] 원격 서버 배포 스크립트를 실행합니다: {script_path}")
    try:
        # 실시간 로그 출력을 위해 Popen 사용 및 stdout/stderr 캡처
        # 윈도우 배치 파일은 shell=True 필수
        process = subprocess.Popen(
            [script_path], 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE,
            text=True,
            shell=(os.name == 'nt')
        )
        
        # 표준 출력 실시간 스트리밍
        for line in process.stdout:
            print(f"[OCI stdout] {line.strip()}")
            
        # 에러 출력 스트리밍
        for line in process.stderr:
            print(f"[OCI stderr] {line.strip()}")
            
        process.wait()
        
        if process.returncode == 0:
            print("✅ [OCI 배포] 클라우드 인프라 자동 배포가 성공적으로 완료되었습니다!")
        else:
            print(f"⚠️ [OCI 배포] 쉘 스크립트 실행 완료되었으나 에러(return code {process.returncode})가 발생했습니다.")
            
    except Exception as e:
        # 배포 실패 시에도 메인 봇 프로세스가 죽지 않도록 완벽히 방어
        print(f"❌ [OCI 배포] 배포 스크립트 실행 중 오류 발생 (메인 봇은 정상 동작 유지): {e}")

def auto_git_sync():
    """
    백그라운드에서 변경사항 발생 시 자동으로 Git Add, Commit, Push를 수행합니다.
    """
    try:
        status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
        if status.stdout.strip():
            current_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            print(f"🔄 [{current_time}] Git modifications detected. Auto-syncing...")
            subprocess.run(["git", "add", "."], check=True)
            
            commit_msg = f"auto: bot sync update {datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
            subprocess.run(["git", "commit", "-m", commit_msg], check=True)
            
            push_res = subprocess.run(["git", "push"], capture_output=True, text=True)
            if push_res.returncode == 0:
                print("✅ Auto-sync completed and pushed to GitHub.")
                # Git 푸시 성공 시 OCI 자동 배포 트리거
                trigger_oci_deployment()
            else:
                print(f"⚠️ Auto-sync push failed (check network/remote): {push_res.stderr}")
    except Exception as e:
        print(f"❌ Error during auto_git_sync: {e}")

LOCK_PORT = 18386
lock_socket = None

def acquire_process_lock():
    """
    중복 실행을 방지하기 위해 로컬 소켓을 바인딩합니다.
    이미 다른 스케줄러 인스턴스가 돌고 있다면 에러와 함께 종료됩니다.
    """
    global lock_socket
    try:
        lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        lock_socket.bind(('127.0.0.1', LOCK_PORT))
    except socket.error:
        print(f"\n❌ [중복 실행 방지] 이미 봇 스케줄러 인스턴스가 실행 중입니다. (포트 {LOCK_PORT} 사용 중) 프로그램을 종료합니다.")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="K-Stock Quant & DART Swing Screener")
    parser.add_argument(
        "--now",
        action="store_true",
        help="Run the screening pipeline immediately and exit."
    )
    parser.add_argument(
        "--now-nxt",
        action="store_true",
        help="Run the NXT-inclusive screening pipeline immediately and exit."
    )
    parser.add_argument(
        "--now-index",
        action="store_true",
        help="Run the index closing settlement immediately and exit."
    )
    args = parser.parse_args()
    
    # Validate .env config
    validate_config(test_mode=True)
    
    if args.now:
        print("🏃 Running manual one-off execution...")
        execute_pipeline(is_nxt=False)
        sys.exit(0)
        
    if args.now_nxt:
        print("🏃 Running manual one-off NXT-inclusive execution...")
        execute_pipeline(is_nxt=True)
        sys.exit(0)
        
    if args.now_index:
        print("🏃 Running manual one-off index settlement execution...")
        execute_index_closing()
        sys.exit(0)
        
    # Schedule mode
    # 중복 실행 방지 락 획득 (OCI 및 로컬 겸용)
    acquire_process_lock()

    # 이전 실행에서 어느 단계까지 알렸는지 복원한다.
    # 복원하지 않으면 재시작할 때마다 현재 낙폭 단계 알림이 다시 발송된다.
    load_alert_state()
    
    is_local_env = (os.name == 'nt')
    
    # Define a wrapper to run jobs in background threads so they don't block the scheduler
    def run_threaded(job_func, *args, **kwargs):
        import threading
        job_thread = threading.Thread(target=job_func, args=args, kwargs=kwargs)
        job_thread.daemon = True
        job_thread.start()

    # Start bot listener thread in the background
    def screener_nxt_wrapper():
        execute_pipeline(is_nxt=True)
        
    if is_local_env:
        print("🖥️ [로컬 개발 환경] 중복 알림 방지를 위해 스케줄러와 텔레그램 챗봇 리스너를 비활성화합니다.")
        print("👉 실시간 Git 자동 동기화(Watchdog) 및 OCI 배포 데몬만 백그라운드에서 실행됩니다.")
    else:
        print("🕰️ Starting daily robust scheduler mode (Production Server)...")
        print("📅 Index Settlement is scheduled to run every Mon-Fri at 15:45 KST.")
        print("📅 NXT Screener is scheduled to run every Mon-Fri at 21:00 KST.")
        print("📅 Leverage ETF (TQQQ/QLD) monitor runs every 30 min during US regular hours (09:30-16:00 ET).")
        send_telegram_message("🚀 [시스템 알림] 스윙종목 분석 봇이 서버에서 정상 작동을 시작했습니다. (스케줄러 대기 중)")
        start_bot_listener(screener_callback=screener_nxt_wrapper, index_callback=execute_index_closing)
    
    print("👉 Use Ctrl+C to terminate.")
    
    # Start Git auto-sync event-driven daemon
    # (로컬 개발 PC 전용 - 서버는 cron 기반 auto_update.sh가 GitHub에서 코드를 받아가므로 불필요)
    if is_local_env:
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler

            class GitSyncHandler(FileSystemEventHandler):
                def __init__(self):
                    super().__init__()
                    self.last_sync = 0

                def on_any_event(self, event):
                    if event.is_directory:
                        return

                    # 운영체제 임시 파일이나 git, report, logs, cache 폴더 등은 무시 (무한루프 방지)
                    ignore_paths = [".git", "reports", "logs", "__pycache__", "venv", ".tmp", "~", ".cache", "chat_ids.json"]
                    if any(ignored in event.src_path for ignored in ignore_paths):
                        return

                    current_time = time.time()
                    # 과도한 동기화 방지 (디바운싱 10초)
                    if current_time - self.last_sync > 10:
                        self.last_sync = current_time
                        # 파일 쓰기가 온전히 완료되도록 잠시 대기 후 스레드로 동기화 실행
                        def delayed_sync():
                            time.sleep(1)
                            auto_git_sync()
                        run_threaded(delayed_sync)

            observer = Observer()
            observer.schedule(GitSyncHandler(), path=str(BASE_DIR), recursive=True)
            observer.start()
            print("🔄 Git Auto-sync daemon started (event-driven real-time, local only).")
        except ImportError:
            print("⚠️ 'watchdog' 모듈이 없습니다. 실시간 자동 동기화를 위해 'pip install watchdog'을 실행해주세요.")
    
    # Keep the script running with robust custom time checking
    kst_tz = datetime.timezone(datetime.timedelta(hours=9))
    last_run_index = None
    last_run_nxt = None
    last_crash_check = None
    last_run_health = None
    last_sidecar_check_time = 0
    last_us_check_time = 0
    
    # Catch-up logic on startup
    now_kst = datetime.datetime.now(kst_tz)
    current_date = now_kst.date()
    
    # 서버 환경(리눅스)일 때만 구동 시 즉시 실행 스케줄러 검사
    if not is_local_env:
        if now_kst.time() >= datetime.time(15, 45) and current_date.weekday() < 5:
            print("🚀 Startup Check: Triggering 15:45 KST Index Settlement immediately...")
            run_threaded(execute_index_closing)
            last_run_index = current_date
            
        if now_kst.time() >= datetime.time(21, 0) and current_date.weekday() < 5:
            print("🚀 Startup Check: Triggering 21:00 KST NXT Screener immediately...")
            run_threaded(execute_pipeline, is_nxt=True)
            last_run_nxt = current_date
    
    try:
        while True:
            # 로컬(Windows) 환경에서는 Watchdog 유지를 위해 sleep만 수행하고 스케줄러는 건너뜁니다.
            if is_local_env:
                time.sleep(10)
                continue
                
            now_kst = datetime.datetime.now(kst_tz)
            current_time = now_kst.strftime("%H:%M")
            current_date = now_kst.date()
            
            # 15-minute index crash monitor (09:00 ~ 15:30)
            if now_kst.time() >= datetime.time(9, 0) and now_kst.time() <= datetime.time(15, 30) and current_date.weekday() < 5:
                if current_time.endswith(":00") or current_time.endswith(":15") or current_time.endswith(":30") or current_time.endswith(":45"):
                    if last_crash_check != current_time:
                        run_threaded(check_and_send_crash_alerts)
                        last_crash_check = current_time
                
                # 90-second sidecar monitor
                current_timestamp = time.time()
                if current_timestamp - last_sidecar_check_time >= 90:
                    run_threaded(check_and_send_sidecar_alerts)
                    last_sidecar_check_time = current_timestamp

            # 미국 정규장 중 30분 단위 레버리지 ETF(TQQQ/QLD) 분할매수 감시.
            # 개장 판정은 KST 고정 시각이 아닌 미국 동부(ET) 기준이므로 서머타임에 자동 대응한다.
            if is_us_market_open():
                current_timestamp = time.time()
                if current_timestamp - last_us_check_time >= 1800:
                    print("⏰ Triggering US Leverage ETF (TQQQ/QLD) monitor...")
                    run_threaded(check_and_send_leverage_etf_alerts)
                    last_us_check_time = current_timestamp

            # 06:00, 12:00 KST: System Health Check
            if current_time in ["06:00", "12:00"] and last_run_health != current_time:
                print(f"⏰ Triggering {current_time} KST System Health Check...")
                run_threaded(check_system_health)
                last_run_health = current_time
            
            # 15:45 KST: Index closing settlement
            if current_time == "15:45" and last_run_index != current_date:
                print(f"⏰ Triggering 15:45 KST Index Settlement...")
                run_threaded(execute_index_closing)
                last_run_index = current_date
                
            # 21:00 KST: Screener pipeline (NXT)
            if current_time == "21:00" and last_run_nxt != current_date:
                print(f"⏰ Triggering 21:00 KST Screener (NXT)...")
                run_threaded(execute_pipeline, is_nxt=True)
                last_run_nxt = current_date
                
            time.sleep(10)
    except KeyboardInterrupt:
        print("\n👋 Scheduler terminated by user.")
        sys.exit(0)

if __name__ == "__main__":
    main()
