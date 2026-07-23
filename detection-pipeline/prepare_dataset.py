"""
prepare_dataset.py
──────────────────
Extracts the Roboflow dataset zip file and converts any OBB (oriented bounding box)
polygon labels (8 numbers: x1 y1 x2 y2 x3 y3 x4 y4) to standard YOLO axis-aligned
bounding box format (4 numbers: x_center y_center w h).
"""

import os
import zipfile
import yaml
from pathlib import Path

ZIP_PATH = Path(r"c:\Users\DELL\Desktop\RakshaRide\Motorcycle helmet.v1i.yolov8-obb.zip")
DATASET_DIR = Path(r"c:\Users\DELL\Desktop\RakshaRide\detection-pipeline\dataset\helmet_dataset")


def convert_obb_line_to_standard(line: str) -> str:
    """
    Convert an OBB line: 'class x1 y1 x2 y2 x3 y3 x4 y4'
    to standard YOLO line: 'class x_center y_center w h'
    Or return unchanged if already standard 5-element format.
    """
    parts = line.strip().split()
    if not parts:
        return ""
    
    cls_id = parts[0]
    coords = [float(p) for p in parts[1:]]

    if len(coords) == 8:
        # Polygon (4 points: x1, y1, x2, y2, x3, y3, x4, y4)
        xs = coords[0::2]
        ys = coords[1::2]
        x_min, x_max = min(xs), max(xs)
        y_min, y_max = min(ys), max(ys)
        
        w = x_max - x_min
        h = y_max - y_min
        x_center = x_min + w / 2.0
        y_center = y_min + h / 2.0
        
        return f"{cls_id} {x_center:.6f} {y_center:.6f} {w:.6f} {h:.6f}"
    elif len(coords) == 4:
        return line.strip()
    else:
        return line.strip()


def extract_and_convert():
    print(f"Extracting '{ZIP_PATH}' to '{DATASET_DIR}'...")
    DATASET_DIR.mkdir(parents=True, exist_ok=True)
    
    with zipfile.ZipFile(ZIP_PATH, 'r') as zip_ref:
        zip_ref.extractall(DATASET_DIR)
        
    print("Extraction complete. Converting labels to standard bounding box format...")
    
    # Process all label text files in labels directories
    converted_count = 0
    label_files = [f for f in DATASET_DIR.rglob("*.txt") if "labels" in f.parts]
    
    for lbl_file in label_files:
        lines = lbl_file.read_text(encoding='utf-8', errors='ignore').splitlines()
        new_lines = []
        for line in lines:
            try:
                converted = convert_obb_line_to_standard(line)
                if converted:
                    new_lines.append(converted)
            except Exception:
                continue
        lbl_file.write_text("\n".join(new_lines) + "\n", encoding='utf-8')
        converted_count += 1

    print(f"Converted {converted_count} label files.")

    # Create / update dataset data.yaml with absolute paths
    data_yaml_path = DATASET_DIR / "data.yaml"
    
    yaml_content = {
        "path": str(DATASET_DIR.resolve()).replace("\\", "/"),
        "train": "train/images",
        "val": "valid/images",
        "test": "test/images",
        "names": {
            0: "helmet",
            1: "no helmet"
        }
    }
    
    with open(data_yaml_path, 'w', encoding='utf-8') as f:
        yaml.dump(yaml_content, f, default_flow_style=False)

    print(f"data.yaml updated at '{data_yaml_path}'")
    print("\nDataset Preparation Complete!")


if __name__ == "__main__":
    extract_and_convert()
