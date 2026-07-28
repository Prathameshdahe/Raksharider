"""
train_helmet_model.py
─────────────────────
Fine-tunes YOLOv8 on the helmet detection dataset using CUDA GPU.

Saves the fine-tuned model to models/helmet_model.pt upon completion.
"""

from pathlib import Path
from ultralytics import YOLO
import torch
import sys
import shutil

DATA_YAML = Path(r"c:\Users\DELL\Desktop\RakshaRide\detection-pipeline\dataset\helmet_dataset\data.yaml")
MODELS_DIR = Path(r"c:\Users\DELL\Desktop\RakshaRide\detection-pipeline\models")


def main():
    print("==================================================")
    print(" RakshaRide — Helmet Model Training (YOLOv8 + CUDA)")
    print("==================================================")

    # 1. Verify CUDA availability
    if not torch.cuda.is_available():
        print("ERROR: CUDA is not available. Please ensure GPU drivers and CUDA-enabled PyTorch are installed.")
        sys.exit(1)

    gpu_name = torch.cuda.get_device_name(0)
    print(f"CUDA Device: {gpu_name} (Device 0)")
    print(f"Dataset YAML: {DATA_YAML}")

    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    # 2. Load base pretrained model (yolov8n.pt for fast, high-accuracy fine-tuning)
    base_model = "yolov8n.pt"
    print(f"Loading pretrained base model: {base_model}...")
    model = YOLO(base_model)

    # 3. Start fine-tuning
    epochs = 25
    batch_size = 16
    imgsz = 640

    print(f"\nStarting training: epochs={epochs}, batch={batch_size}, imgsz={imgsz}, device=0...")
    
    results = model.train(
        data=str(DATA_YAML),
        epochs=epochs,
        batch=batch_size,
        imgsz=imgsz,
        device=0,            # CUDA GPU 0
        workers=4,
        project="runs/detect",
        name="helmet_train",
        exist_ok=True,
        verbose=True
    )

    # 4. Locate best.pt and copy to models/helmet_model.pt
    best_weights = Path(results.save_dir) / "weights" / "best.pt"
    target_weights = MODELS_DIR / "helmet_model.pt"

    if best_weights.exists():
        shutil.copy(best_weights, target_weights)
        print("\n==================================================")
        print(f" SUCCESS: Fine-tuned model saved to:")
        print(f" {target_weights.resolve()}")
        print("==================================================")
    else:
        print(f"WARNING: Trained weights not found at {best_weights}")


if __name__ == "__main__":
    main()
