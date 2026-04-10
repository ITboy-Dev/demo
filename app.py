import gradio as gr
import threading
import time
import os
import main

def start_monitor():
    print("Initializing Hugging Face Continuous Monitor...", flush=True)
    # Override the default 5-minute loop to be infinite for the Hugging Face ecosystem
    main.LOOP_DURATION_SEC = float('inf')
    
    # Wait a few seconds before starting to let the Gradio server bind to 7860 smoothly
    time.sleep(5)
    
    # Run continuous monitor loop exactly as-is from main.py
    main.run_monitor()

# Start the background thread
thread = threading.Thread(target=start_monitor, daemon=True)
thread.start()

# Build a lightweight Gradio interface to keep the pod alive
with gr.Blocks(theme=gr.themes.Monochrome()) as demo:
    gr.Markdown("# 🚀 Freelancer Monitor (Hugging Face Edition)")
    gr.Markdown("""
    ### System Status: **🟢 ONLINE**
    
    This space silently runs your continuous `main.py` Freelancer Monitor script indefinitely in the background! 
    
    **Important Setup Step:** To prevent Hugging Face from putting this Space to sleep, link this page to **cron-job.org** to ping it every 1 minute.
    """)
    alive_btn = gr.Button("Check Background Thread Status")
    output = gr.Textbox(label="Status")
    
    alive_btn.click(lambda: "Monitor Thread is Active and Looping! ✅", inputs=None, outputs=output)

if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=7860)
