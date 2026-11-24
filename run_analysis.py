#!/usr/bin/env python3
"""Starship 遥测数据预处理、信息论评估、聚类分段抽样和存储格式对比。"""

from __future__ import annotations

import bisect
import csv
import gzip
import hashlib
import importlib
import io
import json
import math
import os
import platform
import shutil
import statistics
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

# absolute() 不需要向 Windows 查询目标文件的最终真实路径；在受控目录环境下
# resolve() 可能因 GetFinalPathNameByHandle 权限限制而失败。
ROOT = Path(__file__).absolute().parents[1]
RAW_FILE = ROOT / "data" / "raw" / "telemetry-10fps.csv"
PROCESSED = ROOT / "data" / "processed"
RESULTS = ROOT / "results"
SAMPLED = RESULTS / "sampled"
PLOTS = RESULTS / "plots"
LOGS = ROOT / "logs"
SIGNAL_FIELDS = ("v1", "h1", "v2", "h2")
MISSING = {"", "nan", "na", "null", "none"}
REPEATS = 7
FONT_DIR = ROOT / "assets" / "fonts"
MANIFEST = RESULTS / ".generated_outputs.json"
# 图中实际用到的汉字，用来确认字体确实含中文字形，避免静默输出方块。
FONT_PROBE = "图抽样保留记录比例误差聚类存储耗时对比方法阈值步长原始信号"
SYSTEM_CJK_FONTS = (
    # Windows
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "C:/Windows/Fonts/Deng.ttf",
    # macOS（PingFang 在新版系统中可能不存在，因此还备选冬青黑体与宋体）
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    # Linux
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Regular.otf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
)

def configure_console_utf8():
    """在支持该接口的终端中用 UTF-8 输出中文摘要。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


QUALITY_KEY_ZH = {
    "source_rows": "原始记录数", "clean_rows": "清洗后记录数",
    "removed_fully_empty_rows": "删除的全字段缺失记录数",
    "source_columns": "原始字段", "active_signal_columns": "有效信号字段",
    "fully_missing_columns": "全空信号字段", "missing_by_column": "各字段缺失值数量",
    "column_summary": "各字段统计摘要", "rows_with_at_least_one_signal": "至少包含一个有效信号的记录数",
    "rows_with_time_but_no_signal": "有时间但没有有效信号的记录数",
    "duplicate_full_rows": "完全重复记录数", "repeated_timestamp_extra_rows": "重复整秒时间对应的额外记录数",
    "time_is_nondecreasing_in_source_order": "按原始顺序时间是否非递减",
    "time_field_note": "时间字段说明", "sampling_axis_note": "抽样坐标说明",
    "input_sha256": "输入文件SHA256校验值", "input_file_bytes": "输入文件大小（字节）",
    "source_link": "数据来源链接", "source_method_note": "数据提取方式说明",
    "present": "非缺失值数量", "missing": "缺失值数量", "minimum": "最小值",
    "maximum": "最大值", "unique_values": "不同取值数量",
}


def chinese_quality_summary(value):
    """递归翻译质量摘要的字段名；数据源字段名和链接保持原样。"""
    if isinstance(value, dict):
        return {QUALITY_KEY_ZH.get(key, key): chinese_quality_summary(item) for key, item in value.items()}
    if isinstance(value, list):
        return [chinese_quality_summary(item) for item in value]
    return value


def chinese_sampling_rows(rows):
    output = []
    for row in rows:
        output.append({
            "抽样方法": row["label"],
            "方法类别": "固定间隔抽样" if row["family"] == "fixed" else (
                "数值聚类组分段固定抽样" if row["family"] == "cluster" else "误差约束自适应抽样"),
            "固定抽样步长": row["step"],
            "自适应误差阈值（%）": row["epsilon_pct"],
            "保留记录数": row["sample_rows"],
            "含有效信号记录数": row["observed_signal_rows"],
            "保留比例（%）": row["retention_pct"],
            "抽样CSV大小（字节）": row["csv_bytes"],
            "抽样Gzip CSV大小（字节）": row["gzip_bytes"],
            "最大归一化重建误差（%）": row["max_error_pct_of_channel_range"],
            "归一化均方根误差（%）": row["rmse_pct_of_channel_range"],
            "KL散度（v1）": row.get("kl_v1", ""),
            "KL散度（h1）": row.get("kl_h1", ""),
            "熵比值_v1（%）": row.get("entropy_ratio_v1", ""),
            "熵比值_h1（%）": row.get("entropy_ratio_h1", ""),
        })
    return output


def chinese_storage_rows(rows):
    format_names = {"CSV": "CSV（逗号分隔文本）", "Gzip CSV": "Gzip压缩CSV", "Parquet": "Parquet（列式存储）"}
    compression_names = {"none": "无压缩", "gzip": "Gzip", "zstd": "Zstandard（Zstd）"}
    output = []
    for row in rows:
        output.append({
            "存储格式": format_names.get(row["format"], row["format"]),
            "状态": "可用" if row["status"] == "available" else "未生成（缺少依赖）",
            "压缩方式": compression_names.get(row["compression"], row["compression"]),
            "文件大小（字节）": row["bytes"],
            "写入耗时中位数（毫秒）": row["write_median_ms"],
            "读取耗时中位数（毫秒）": row["read_median_ms"],
            "清洗后记录数": row["clean_row_count"],
            "原始CSV大小（字节）": row["source_csv_bytes_reference"],
            "基准重复次数": row["benchmark_repeats"],
            "备注": "未安装 PyArrow，未生成 Parquet 文件。" if row["status"] != "available" else "",
        })
    return output


def storage_name_zh(name):
    return {"CSV": "CSV（逗号分隔文本）", "Gzip CSV": "Gzip压缩CSV", "Parquet": "Parquet（列式存储）"}.get(name, name)


def number(value):
    if value is None or str(value).strip().lower() in MISSING:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def load_raw(path):
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        records = []
        for sample_index, source in enumerate(reader):
            row = {"sample_index": sample_index}
            for field in fields:
                row[field] = number(source.get(field))
            records.append(row)
    return fields, records


def format_number(value):
    if value is None:
        return ""
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(numeric_value):
        return ""
    if numeric_value.is_integer():
        return str(int(numeric_value))
    return format(numeric_value, ".12g")


def write_csv(path, rows, columns, excel_friendly=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    encoding = "utf-8-sig" if excel_friendly else "utf-8"
    with path.open("w", encoding=encoding, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: format_number(row.get(key)) for key in columns})


def read_csv(path):
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def read_gzip_csv(path):
    with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def write_gzip_csv(path, rows, columns):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as raw_handle:
        with gzip.GzipFile(fileobj=raw_handle, mode="wb", mtime=0) as compressed:
            with io.TextIOWrapper(compressed, encoding="utf-8", newline="") as text:
                writer = csv.DictWriter(text, fieldnames=columns, extrasaction="ignore")
                writer.writeheader()
                for row in rows:
                    writer.writerow({key: format_number(row.get(key)) for key in columns})


def contiguous_runs(rows, field):
    runs = []
    current = []
    for index, row in enumerate(rows):
        if row.get(field) is None:
            if current:
                runs.append(current)
                current = []
        else:
            current.append(index)
    if current:
        runs.append(current)
    return runs


def turning_points(values, min_delta):
    anchors = set()
    previous_sign = 0
    for right in range(1, len(values)):
        difference = values[right] - values[right - 1]
        if abs(difference) < min_delta:
            continue
        sign = 1 if difference > 0 else -1
        if previous_sign and sign != previous_sign:
            anchors.add(right - 1)
        previous_sign = sign
    return anchors


def adaptive_segment(rows, run, field, epsilon, value_range):
    values = [rows[index][field] for index in run]
    x_values = [rows[index]["sample_index"] for index in run]
    keep = {0, len(run) - 1}
    if len(run) <= 2 or value_range <= 0:
        return {run[position] for position in keep}
    min_delta = value_range * 0.005
    keep.update(turning_points(values, min_delta))
    keep.add(min(range(len(values)), key=values.__getitem__))
    keep.add(max(range(len(values)), key=values.__getitem__))
    anchors = sorted(keep)
    stack = list(zip(anchors[:-1], anchors[1:]))
    while stack:
        left, right = stack.pop()
        if right - left <= 1:
            continue
        x0, x1 = x_values[left], x_values[right]
        y0, y1 = values[left], values[right]
        max_error = -1.0
        split = None
        for position in range(left + 1, right):
            fraction = (x_values[position] - x0) / (x1 - x0)
            predicted = y0 + fraction * (y1 - y0)
            error = abs(values[position] - predicted) / value_range
            if error > max_error:
                max_error = error
                split = position
        if split is not None and max_error > epsilon:
            keep.add(split)
            stack.extend(((left, split), (split, right)))
    return {run[position] for position in keep}


def fixed_selection(rows, channels, step):
    """按有效记录的全局顺序抽样，再补齐各信号连续有效段的端点。"""
    observed = [
        index for index, row in enumerate(rows)
        if any(row.get(field) is not None for field in channels)
    ]
    selected = set(observed[::step])
    if observed:
        selected.add(observed[-1])
    for field in channels:
        for run in contiguous_runs(rows, field):
            selected.add(run[0])
            selected.add(run[-1])
    return selected


def adaptive_selection(rows, channels, epsilon):
    selected = set()
    ranges = {}
    for field in channels:
        values = [row[field] for row in rows if row.get(field) is not None]
        value_range = max(values) - min(values) if values else 0.0
        ranges[field] = value_range
        for run in contiguous_runs(rows, field):
            selected.update(adaptive_segment(rows, run, field, epsilon, value_range))

    # 合并各信号独立选出的点后，公共锚点可能改变另一路信号的重建误差。
    # 因此反复检查最终共享索引集，直到每路信号均满足阈值。
    while True:
        violations = set()
        for field in channels:
            value_range = ranges[field]
            if value_range <= 0:
                continue
            for run in contiguous_runs(rows, field):
                run_set = set(run)
                anchors = sorted(index for index in selected if index in run_set)
                for left, right in zip(anchors[:-1], anchors[1:]):
                    if right - left <= 1:
                        continue
                    x0 = rows[left]["sample_index"]
                    x1 = rows[right]["sample_index"]
                    y0 = rows[left][field]
                    y1 = rows[right][field]
                    worst_error = -1.0
                    worst_index = None
                    for index in range(left + 1, right):
                        x = rows[index]["sample_index"]
                        fraction = (x - x0) / (x1 - x0)
                        predicted = y0 + fraction * (y1 - y0)
                        error = abs(rows[index][field] - predicted) / value_range
                        if error > worst_error:
                            worst_error = error
                            worst_index = index
                    if worst_index is not None and worst_error > epsilon:
                        violations.add(worst_index)
        if not violations:
            break
        previous_count = len(selected)
        selected.update(violations)
        if len(selected) == previous_count:
            break
    return selected

# ================= 新增：信息论与聚类模块 =================

def calc_entropy(values, bins=30, value_range=None):
    """使用指定范围的等宽分箱计算信息熵；比较样本时应共用同一分箱范围。"""
    if not values:
        return 0.0
    if bins <= 0:
        raise ValueError("bins 必须是正整数")
    if value_range is None:
        v_min, v_max = min(values), max(values)
    else:
        v_min, v_max = value_range
    if v_max <= v_min:
        return 0.0
    step = (v_max - v_min) / bins
    counts = [0] * bins
    for value in values:
        index = max(0, min(int((value - v_min) / step), bins - 1))
        counts[index] += 1
    total = len(values)
    return -sum(
        (count / total) * math.log2(count / total)
        for count in counts if count > 0
    )

def calc_mutual_information(x_vals, y_vals, bins=20):
    """计算两个连续变量的互信息"""
    if not x_vals or len(x_vals) != len(y_vals):
        return 0.0
    x_min, x_max = min(x_vals), max(x_vals)
    y_min, y_max = min(y_vals), max(y_vals)
    if x_max == x_min or y_max == y_min:
        return 0.0
    x_step = (x_max - x_min) / bins
    y_step = (y_max - y_min) / bins
    joint_counts = [[0] * bins for _ in range(bins)]
    for x, y in zip(x_vals, y_vals):
        xi = min(int((x - x_min) / x_step), bins - 1)
        yi = min(int((y - y_min) / y_step), bins - 1)
        joint_counts[xi][yi] += 1
    total = len(x_vals)
    mi = 0.0
    for i in range(bins):
        for j in range(bins):
            c = joint_counts[i][j]
            if c > 0:
                p_xy = c / total
                p_x = sum(joint_counts[i]) / total
                p_y = sum(row[j] for row in joint_counts) / total
                mi += p_xy * math.log2(p_xy / (p_x * p_y))
    return max(0.0, mi)


def calc_kl_divergence(orig_values, sampled_values, bins=30, alpha=0.5):
    """用原始数据范围和加性平滑计算 KL(P原始 || Q抽样)，单位为比特。"""
    if not orig_values or not sampled_values:
        return 0.0
    if bins <= 0 or alpha <= 0:
        raise ValueError("bins 和 alpha 必须为正数")

    v_min, v_max = min(orig_values), max(orig_values)
    if v_max <= v_min:
        return 0.0
    step = (v_max - v_min) / bins
    p_counts = [0] * bins
    q_counts = [0] * bins
    for value in orig_values:
        index = max(0, min(int((value - v_min) / step), bins - 1))
        p_counts[index] += 1
    for value in sampled_values:
        index = max(0, min(int((value - v_min) / step), bins - 1))
        q_counts[index] += 1

    p_denominator = len(orig_values) + alpha * bins
    q_denominator = len(sampled_values) + alpha * bins
    p_probs = [(count + alpha) / p_denominator for count in p_counts]
    q_probs = [(count + alpha) / q_denominator for count in q_counts]
    divergence = sum(p * math.log2(p / q) for p, q in zip(p_probs, q_probs))
    return max(0.0, divergence)

def cluster_segments(rows, channels):
    """基于 K-Means++ 对两个信号聚类，生成数值聚类组编号。"""
    if len(channels) < 2:
        print("提示：有效信号不足两个，跳过 K-Means++ 数值聚类组分析。")
        return [], []
    try:
        import numpy as np
        from sklearn.cluster import KMeans
        from sklearn.preprocessing import StandardScaler
    except ImportError as error:
        print(f"提示：缺少聚类依赖（{error}），跳过 K-Means++ 数值聚类组分析；请安装 requirements.txt。")
        return [], []

    # 提取两个有效信号同时存在的点
    valid_idx = [i for i, r in enumerate(rows) if all(r.get(c) is not None for c in channels)]
    if len(valid_idx) < 10:
        print("提示：两个信号同时有效的记录不足 10 条，跳过 K-Means++ 数值聚类组分析。")
        return [], []

    data = np.array([[rows[i][c] for c in channels] for i in valid_idx])
    scaler = StandardScaler()
    data_scaled = scaler.fit_transform(data)
    if np.unique(data_scaled, axis=0).shape[0] < 5:
        print("提示：v1、h1 的有效数值组合少于 5 种，无法按 K=5 聚类，跳过数值聚类组分析。")
        return [], []

    # K-means++
    k = 5
    kmeans = KMeans(n_clusters=k, init='k-means++', n_init=10, random_state=42)
    labels = kmeans.fit_predict(data_scaled)

    # 映射回原行
    cluster_labels = [None] * len(rows)
    for idx, label in zip(valid_idx, labels):
        cluster_labels[idx] = int(label)

    # 提取连续段
    segments = []
    current_seg = []
    current_label = None
    for i, label in enumerate(cluster_labels):
        if label is None:
            if current_seg:
                segments.append({"label": current_label, "indices": current_seg})
                current_seg = []
            current_label = None
            continue
        if current_label is None:
            current_label = label
        if label != current_label:
            segments.append({"label": current_label, "indices": current_seg})
            current_seg = []
            current_label = label
        current_seg.append(i)
    if current_seg:
        segments.append({"label": current_label, "indices": current_seg})

    return cluster_labels, segments


def cluster_fixed_selection(rows, channels, segments, step):
    """在每个连续同一数值聚类组的片段内固定间隔抽样，并保留片段边界点。"""
    selected = set()
    for seg in segments:
        indices = seg["indices"]
        if not indices:
            continue
        # 只处理该段内两个信号都有的点
        valid_in_seg = [i for i in indices if all(rows[i].get(c) is not None for c in channels)]
        if not valid_in_seg:
            continue
        selected.update(valid_in_seg[::step])
        selected.add(valid_in_seg[0])
        selected.add(valid_in_seg[-1])

    # 补充缺失段的端点，避免抽样结果完全丢掉 v1 单独存在的地方
    for field in channels:
        for run in contiguous_runs(rows, field):
            selected.add(run[0])
            selected.add(run[-1])
    return selected


# ================= 新增结束 =================


def channel_errors(rows, selected, field):
    errors = []
    selected = set(selected)
    for run in contiguous_runs(rows, field):
        run_set = set(run)
        anchors = sorted(index for index in selected if index in run_set)
        if not anchors:
            continue
        x_anchors = [rows[index]["sample_index"] for index in anchors]
        y_anchors = [rows[index][field] for index in anchors]
        for index in run:
            x = rows[index]["sample_index"]
            right = bisect.bisect_left(x_anchors, x)
            if right < len(x_anchors) and x_anchors[right] == x:
                predicted = y_anchors[right]
            elif right == 0:
                predicted = y_anchors[0]
            elif right == len(x_anchors):
                predicted = y_anchors[-1]
            else:
                left = right - 1
                fraction = (x - x_anchors[left]) / (x_anchors[right] - x_anchors[left])
                predicted = y_anchors[left] + fraction * (y_anchors[right] - y_anchors[left])
            errors.append(rows[index][field] - predicted)
    return errors


def sampling_metrics(rows, channels, selected, orig_entropy):
    all_normalized_errors = []
    per_channel = {}
    for field in channels:
        values = [row[field] for row in rows if row.get(field) is not None]
        value_range = max(values) - min(values) if values else 0.0
        errors = channel_errors(rows, selected, field)
        max_abs = max((abs(error) for error in errors), default=0.0)
        rmse = math.sqrt(sum(error * error for error in errors) / len(errors)) if errors else 0.0
        normalized = [abs(error) / value_range for error in errors] if value_range else [0.0 for _ in errors]
        all_normalized_errors.extend(normalized)
        per_channel[field] = {
            "observed_values": len(values),
            "range": value_range,
            "max_abs_error": max_abs,
            "rmse": rmse,
            "max_error_pct_of_range": (max_abs / value_range * 100) if value_range else 0.0,
        }

    # 分布指标基于同一批原始有效值确定的范围与 30 个等宽箱。
    original_v1 = [row["v1"] for row in rows if row.get("v1") is not None]
    sampled_v1 = [rows[index]["v1"] for index in selected if rows[index].get("v1") is not None]
    original_h1 = [row["h1"] for row in rows if row.get("h1") is not None]
    sampled_h1 = [rows[index]["h1"] for index in selected if rows[index].get("h1") is not None]
    kl_v1 = calc_kl_divergence(original_v1, sampled_v1)
    kl_h1 = calc_kl_divergence(original_h1, sampled_h1)

    range_v1 = (min(original_v1), max(original_v1)) if original_v1 else None
    range_h1 = (min(original_h1), max(original_h1)) if original_h1 else None
    sampled_ent_v1 = calc_entropy(sampled_v1, value_range=range_v1)
    sampled_ent_h1 = calc_entropy(sampled_h1, value_range=range_h1)
    ret_v1 = (sampled_ent_v1 / orig_entropy["v1"] * 100) if orig_entropy["v1"] > 0 else 0.0
    ret_h1 = (sampled_ent_h1 / orig_entropy["h1"] * 100) if orig_entropy["h1"] > 0 else 0.0

    overall_max = max(all_normalized_errors, default=0.0) * 100
    overall_rmse = math.sqrt(sum(value * value for value in all_normalized_errors) / len(
        all_normalized_errors)) * 100 if all_normalized_errors else 0.0

    return {
        "max_error_pct_of_channel_range": overall_max,
        "rmse_pct_of_channel_range": overall_rmse,
        "per_channel": per_channel,
        "kl_v1": kl_v1,
        "kl_h1": kl_h1,
        "entropy_ratio_v1": ret_v1,
        "entropy_ratio_h1": ret_h1,
    }

def quality_summary(fields, raw_records, clean_records, active_channels, dropped_fields):
    missing_counts = {}
    value_summaries = {}
    for field in fields:
        values = [row.get(field) for row in raw_records]
        present = [value for value in values if value is not None]
        missing_counts[field] = len(values) - len(present)
        value_summaries[field] = {
            "present": len(present),
            "missing": len(values) - len(present),
            "minimum": min(present) if present else None,
            "maximum": max(present) if present else None,
            "unique_values": len(set(present)),
        }
    timestamps = [row["t"] for row in raw_records if row.get("t") is not None]
    timestamp_counts = Counter(timestamps)
    duplicate_timestamp_extras = sum(count - 1 for count in timestamp_counts.values())
    seen = set()
    duplicate_rows = 0
    for row in raw_records:
        key = tuple(row.get(field) for field in fields)
        if key in seen:
            duplicate_rows += 1
        else:
            seen.add(key)
    signal_rows = sum(any(row.get(field) is not None for field in active_channels) for row in clean_records)
    return {
        "source_rows": len(raw_records),
        "clean_rows": len(clean_records),
        "removed_fully_empty_rows": len(raw_records) - len(clean_records),
        "source_columns": fields,
        "active_signal_columns": active_channels,
        "fully_missing_columns": dropped_fields,
        "missing_by_column": missing_counts,
        "column_summary": value_summaries,
        "rows_with_at_least_one_signal": signal_rows,
        "rows_with_time_but_no_signal": sum(
            row.get("t") is not None and not any(row.get(field) is not None for field in active_channels) for row in
            clean_records),
        "duplicate_full_rows": duplicate_rows,
        "repeated_timestamp_extra_rows": duplicate_timestamp_extras,
        "time_is_nondecreasing_in_source_order": all(left <= right for left, right in zip(timestamps, timestamps[1:])),
        "time_field_note": "数据中的 t 字段按整秒记录；对于每秒 10 帧的数据，同一 t 值重复出现属于预期情况。",
        "sampling_axis_note": "插值与曲线重建按原始记录顺序 sample_index 进行，不使用取整后的 t 作为横坐标。",
    }


def median_ms(operation, repeats=REPEATS):
    measurements = []
    for _ in range(repeats):
        started = time.perf_counter()
        operation()
        measurements.append((time.perf_counter() - started) * 1000)
    return round(statistics.median(measurements), 4)


def benchmark_storage(clean_rows, columns, raw_size):
    output_csv = PROCESSED / "telemetry_clean.csv"
    output_gzip = PROCESSED / "telemetry_clean.csv.gz"
    write_csv(output_csv, clean_rows, columns)
    write_gzip_csv(output_gzip, clean_rows, columns)
    rows_out = []
    csv_path = PROCESSED / "__benchmark.csv"
    gzip_path = PROCESSED / "__benchmark.csv.gz"
    rows_out.append({
        "format": "CSV", "status": "available", "bytes": output_csv.stat().st_size,
        "write_median_ms": median_ms(lambda: write_csv(csv_path, clean_rows, columns)),
        "read_median_ms": median_ms(lambda: read_csv(csv_path)), "compression": "none",
    })
    rows_out.append({
        "format": "Gzip CSV", "status": "available", "bytes": output_gzip.stat().st_size,
        "write_median_ms": median_ms(lambda: write_gzip_csv(gzip_path, clean_rows, columns)),
        "read_median_ms": median_ms(lambda: read_gzip_csv(gzip_path)), "compression": "gzip",
    })
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq

        parquet_path = PROCESSED / "__benchmark.parquet"
        table = pa.table({column: [row.get(column) for row in clean_rows] for column in columns})
        final_parquet = PROCESSED / "telemetry_clean.parquet"
        pq.write_table(table, final_parquet, compression="zstd")
        rows_out.append({
            "format": "Parquet", "status": "available", "bytes": final_parquet.stat().st_size,
            "write_median_ms": median_ms(lambda: pq.write_table(table, parquet_path, compression="zstd")),
            "read_median_ms": median_ms(lambda: pq.read_table(parquet_path)), "compression": "zstd",
        })
    except ImportError:
        rows_out.append({
            "format": "Parquet", "status": "skipped", "bytes": "",
            "write_median_ms": "", "read_median_ms": "", "compression": "zstd",
            "note": "安装 requirements.txt 中的 PyArrow 依赖后可生成 Parquet 文件。",
        })
    for temporary_path in (csv_path, gzip_path, PROCESSED / "__benchmark.parquet"):
        temporary_path.unlink(missing_ok=True)
    for row in rows_out:
        row["clean_row_count"] = len(clean_rows)
        row["source_csv_bytes_reference"] = raw_size
        row["benchmark_repeats"] = REPEATS
    return rows_out


def font_has_cjk(path, probe=FONT_PROBE):
    """检查字体是否包含图中需要的汉字；无法检查时返回 True，避免因缺少 fontTools 阻断出图。"""
    try:
        from fontTools.ttLib import TTCollection, TTFont
    except ImportError:
        return True
    try:
        if Path(path).suffix.lower() == ".ttc":
            font = TTCollection(str(path)).fonts[0]
        else:
            font = TTFont(str(path), fontNumber=0, lazy=True)
        cmap = font.getBestCmap() or {}
    except Exception:
        return False
    return all(ord(char) in cmap for char in probe)


def bundled_font_files():
    """项目自带字体（SIL OFL 1.1，含常规与粗体两个字重）。"""
    if not FONT_DIR.is_dir():
        return []
    return sorted(
        path for path in FONT_DIR.glob("*.ttf")
        if not path.name.startswith("._")
    )


def resolve_cjk_font():
    """按“环境变量 → 自带字体 → 系统字体”的顺序选一组确实含中文字形的字体。

    自带字体随仓库分发（assets/fonts/），换机器或换操作系统都不用另外安装中文字体；
    系统字体只作为兜底，选中前会用 FONT_PROBE 检查字形覆盖，避免静默输出方块。
    """
    override = os.environ.get("STARSHIP_TELEMETRY_FONT", "").strip()
    font_sets = []
    if override:
        font_sets.append([Path(override)])
    if bundled_font_files():
        font_sets.append(bundled_font_files())
    font_sets.extend([[Path(path)] for path in SYSTEM_CJK_FONTS])

    for paths in font_sets:
        if not paths or not all(path.is_file() for path in paths):
            continue
        if not all(font_has_cjk(path) for path in paths):
            print(f"提示：字体 {paths[0]} 缺少部分中文字形，已跳过。")
            continue
        try:
            from matplotlib import font_manager
            for path in paths:
                font_manager.fontManager.addfont(str(path))
            name = font_manager.FontProperties(fname=str(paths[0])).get_name()
        except Exception as error:
            print(f"提示：字体 {paths[0]} 加载失败（{error}），已跳过。")
            continue
        return paths, name
    return [], None


def write_text_resilient(path, text, attempts=3):
    """写辅助文本文件：先写同目录临时文件再整体替换，遇到瞬时占用会重试。

    之所以不用 path.write_text()：Windows 的 create-always 语义（即 open(path, "w")）
    在目标文件带「隐藏」属性时会直接返回“拒绝访问”，Python 报成 PermissionError [Errno 13]。
    项目从 macOS 拷到 exFAT 移动硬盘时，点开头的文件（如 results/.generated_outputs.json）
    会被打上隐藏属性，于是每次运行都在收尾这一步失败。临时文件 + os.replace 不受该属性影响，
    顺带让写入变成原子的：移动硬盘中途被拔不会留下半个文件。

    该函数只用于清理清单这类辅助文件：写失败不应让已经完成的实验以退出码 1 结束，
    所以错误交回调用方，由调用方决定只打印提示。
    """
    error = None
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    for attempt in range(attempts):
        try:
            temporary.write_text(text, encoding="utf-8")
            os.replace(temporary, path)
            return True, None
        except OSError as caught:
            error = caught
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            try:
                # 目标文件若是只读，replace 会失败；这里顺手清掉只读属性再重试。
                os.chmod(path, 0o666)
            except OSError:
                pass
            time.sleep(0.3 * (attempt + 1))
    return False, error


def prune_stale_outputs(keep_sampled, keep_plots):
    """清理上一次运行留下、本次不再生成的抽样文件与图表，以及 macOS 元数据文件。

    只删除上次运行清单（results/.generated_outputs.json）里记录过的文件，
    不会碰手工放进 results/ 的图（例如自己画的专利流程图）。
    清理失败只给出提示，不中断主流程（只读目录、被占用文件等都可能失败）。
    """
    removed_outputs = []

    def remove(path, is_metadata=False):
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as error:
            print(f"提示：无法删除过期文件 {path}（{error}），请手动清理。")
            return
        if not is_metadata:
            removed_outputs.append(path)

    try:
        previous = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    stale = {
        SAMPLED: (set(previous.get("sampled", [])) - set(keep_sampled), (".csv.gz", ".csv")),
        PLOTS: (set(previous.get("plots", [])) - set(keep_plots), (".png",)),
    }
    for directory, (names, suffixes) in stale.items():
        for name in sorted(names):
            for suffix in suffixes:
                path = directory / f"{name}{suffix}"
                if path.is_file():
                    remove(path)
    for leftover in list(RESULTS.glob(".telemetry-bench-*")) + list(RESULTS.glob("__benchmark*")):
        remove(leftover)
    written, error = write_text_resilient(
        MANIFEST,
        json.dumps({"sampled": sorted(keep_sampled), "plots": sorted(keep_plots)},
                   ensure_ascii=False, indent=2) + "\n",
    )
    if not written:
        print(f"提示：无法写入清理清单 {MANIFEST}（{error}）。")
        print("      本次结果已全部生成，只影响下次运行对过期文件的清理；请检查该文件是否被占用或设为只读。")
    for metadata in sorted(ROOT.rglob("._*")) + sorted(ROOT.rglob(".DS_Store")):
        remove(metadata, is_metadata=True)
    if removed_outputs:
        print(f"已清理上次运行遗留的输出 {len(removed_outputs)} 个文件。")
    return removed_outputs


def make_pngs(rows, channels, sample_sets, sampling_results, storage_results, cluster_labels, segments):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager
        from matplotlib.lines import Line2D
        from matplotlib.patches import Patch, Rectangle
    except ImportError:
        return []

    PLOTS.mkdir(parents=True, exist_ok=True)
    font_paths, font_name = resolve_cjk_font()
    if font_name is not None:
        plt.rcParams["font.family"] = [font_name, "DejaVu Sans"]
        plt.rcParams["font.sans-serif"] = [font_name, "DejaVu Sans"]
        print(f"绘图中文字体：{font_name}（{font_paths[0].parent}，{len(font_paths)} 个字重）")
    else:
        plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "PingFang SC",
                                           "Hiragino Sans GB", "Noto Sans CJK SC", "DejaVu Sans"]
        print("警告：未找到可用的中文字体，图中的中文会显示为方块。")
        print("      请安装思源黑体/Noto Sans CJK，或将字体文件放到 assets/fonts/ 目录。")
    plt.rcParams["axes.unicode_minus"] = False
    plt.rcParams["figure.facecolor"] = "white"
    plt.rcParams["axes.facecolor"] = "white"
    plt.rcParams["savefig.facecolor"] = "white"
    plt.rcParams["text.color"] = "black"
    plt.rcParams["axes.labelcolor"] = "black"
    plt.rcParams["axes.edgecolor"] = "black"
    plt.rcParams["xtick.color"] = "black"
    plt.rcParams["ytick.color"] = "black"
    plt.rcParams["axes.grid"] = False
    plt.rcParams["axes.linewidth"] = 0.8
    cluster_markers = ["o", "s", "^", "D", "v"]
    cluster_hatches = ["", "/", "\\\\", "xx", ".."]
    made = []

    def style_axes(ax):
        ax.grid(False)
        ax.tick_params(direction="out", length=3, width=0.8, colors="black")
        for spine in ax.spines.values():
            spine.set_color("black")
            spine.set_linewidth(0.8)

    def draw_channel(ax, field, linestyle="-"):
        """按连续有效区段画原始信号，缺失位置不跨段连线。"""
        for run in contiguous_runs(rows, field):
            xs = [rows[index]["sample_index"] for index in run]
            ys = [rows[index][field] for index in run]
            ax.plot(xs, ys, color="black", linewidth=1.0, linestyle=linestyle, zorder=2)

    def selected_xy(selected, field):
        indices = [index for index in sorted(selected) if rows[index].get(field) is not None]
        return (
            [rows[index]["sample_index"] for index in indices],
            [rows[index][field] for index in indices],
        )

    def finish_figure(fig, path):
        fig.savefig(path, dpi=300, bbox_inches="tight", facecolor="white")
        plt.close(fig)
        made.append(path)

    # 1. 黑白曲线图：空心圆表示被保留的数据记录。
    panel_channels = channels[:2]
    if panel_channels:
        selected = sample_sets.get("adaptive_eps_1pct", set())
        fig, axes = plt.subplots(len(panel_channels), 1, figsize=(12, 8), sharex=True, squeeze=False)
        fig.suptitle("图1 遥测曲线抽样示意图", fontsize=15, fontweight="bold", y=0.99, color="black")
        for panel, field in enumerate(panel_channels):
            ax = axes[panel, 0]
            draw_channel(ax, field)
            sx, sy = selected_xy(selected, field)
            ax.scatter(sx, sy, s=18, facecolor="white", edgecolor="black", linewidth=0.75,
                       zorder=4, label="保留记录")
            ax.set_ylabel(field)
            style_axes(ax)
            ax.margins(x=0.01, y=0.08)
            if panel == 0:
                ax.legend(handles=[
                    Line2D([0], [0], color="black", lw=1.0, label="原始记录"),
                    Line2D([0], [0], marker="o", color="black", markerfacecolor="white",
                           markeredgecolor="black", markersize=5, linestyle="None", label="保留记录"),
                ], loc="upper left", frameon=True, facecolor="white", edgecolor="black", ncol=2)
        axes[-1, 0].set_xlabel("原始记录序号")
        fig.tight_layout(rect=[0.04, 0.04, 1, 0.94])
        path = PLOTS / "telemetry_adaptive_1pct.png"
        finish_figure(fig, path)

    # 2. 黑白抽样权衡图：三类方法分面、共用坐标尺度。
    family_specs = [
        ("fixed", "固定间隔抽样"),
        ("adaptive", "误差约束自适应抽样"),
        ("cluster", "数值聚类组分段抽样"),
    ]
    max_retention = max((item["retention_pct"] for item in sampling_results), default=50.0)
    x_limit = max(55.0, math.ceil(max_retention / 10) * 10 + 5)
    max_error = max((item["max_error_pct_of_channel_range"] for item in sampling_results), default=2.5)
    y_limit = max(2.5, math.ceil(max_error * 2) / 2 + 0.2)
    def place_label(ax, x, y, text):
        """把标签放在数据点旁；靠近坐标轴右/上边缘时自动翻转到另一侧，避免标注重叠。"""
        x0, x1 = ax.get_xlim()
        y0, y1 = ax.get_ylim()
        fraction_x = (x - x0) / (x1 - x0) if x1 > x0 else 0.5
        fraction_y = (y - y0) / (y1 - y0) if y1 > y0 else 0.5
        dx, dy, horizontal, vertical = 6, 6, "left", "bottom"
        if fraction_x > 0.72:
            dx, horizontal = -6, "right"
        if fraction_y > 0.80:
            dy, vertical = -6, "top"
        ax.annotate(text, (x, y), xytext=(dx, dy), textcoords="offset points", fontsize=8,
                    ha=horizontal, va=vertical, color="black", zorder=5,
                    bbox={"boxstyle": "square,pad=0.12", "fc": "white", "ec": "black", "lw": 0.45})

    fig, axes = plt.subplots(1, 3, figsize=(17, 7), sharex=True, sharey=True)
    fig.suptitle("图2 抽样保留率与重建误差关系图", fontsize=15, fontweight="bold", y=0.99,
                 color="black")
    for ax, (family, title) in zip(axes, family_specs):
        items = [item for item in sampling_results if item["family"] == family]
        ax.set_title(title, fontsize=11, fontweight="bold", pad=9, color="black")
        ax.set_xlim(0, x_limit)
        ax.set_ylim(0, y_limit)
        ax.set_xticks(list(range(0, int(x_limit) + 1, 10)))
        ax.set_yticks([tick * 0.5 for tick in range(int(y_limit * 2) + 1)])
        for item in items:
            x = item["retention_pct"]
            y = item["max_error_pct_of_channel_range"]
            ax.scatter([x], [y], s=34, color="black", marker="o", zorder=3)
            parameter = item["step"] if family != "adaptive" else item["epsilon_pct"]
            if family == "adaptive":
                label = f"ε={float(parameter):g}%"
            else:
                label = f"n={int(parameter)}"
            place_label(ax, x, y, label)
        style_axes(ax)
        ax.set_xlabel("保留比例（%）")
    axes[0].set_ylabel("最大重建误差（%）")
    fig.tight_layout(rect=[0.04, 0.04, 1, 0.94], w_pad=1.5)
    path = PLOTS / "sampling_tradeoff.png"
    finish_figure(fig, path)

    # 3. 黑白聚类散点图：使用点形区分数值聚类组。
    if cluster_labels and len([c for c in channels]) >= 2:
        valid_idx = [i for i, r in enumerate(rows) if all(r.get(c) is not None for c in channels[:2])]
        if valid_idx:
            fig, ax = plt.subplots(figsize=(9, 7))
            for label in sorted({cluster_labels[i] for i in valid_idx if cluster_labels[i] is not None}):
                indices = [i for i in valid_idx if cluster_labels[i] == label]
                ax.scatter([rows[i][channels[0]] for i in indices], [rows[i][channels[1]] for i in indices],
                           s=5, marker=cluster_markers[label % len(cluster_markers)],
                           facecolors="white", edgecolors="black", linewidths=0.35, label=f"组 {label}")
            ax.set_title("图3 数值聚类组分布图", fontsize=15, fontweight="bold", pad=12, color="black")
            ax.set_xlabel(channels[0])
            ax.set_ylabel(channels[1])
            style_axes(ax)
            ax.legend(title="聚类组编号", loc="upper left", frameon=True, facecolor="white",
                      edgecolor="black", ncol=1)
            fig.tight_layout(rect=[0.04, 0.04, 1, 0.96])
            path = PLOTS / "kmeans_cluster_scatter.png"
            finish_figure(fig, path)

    # 4. 黑白数值聚类组分段图：用线条纹理区分不同聚类组，空心圆标出保留记录。
    if segments and panel_channels:
        selected = sample_sets.get("cluster_step_5", set())
        fig, axes = plt.subplots(3, 1, figsize=(12, 9), sharex=True,
                                 gridspec_kw={"height_ratios": [0.55, 1.5, 1.5]})
        fig.suptitle("图4 数值聚类组分段抽样示意图", fontsize=15, fontweight="bold", y=0.99, color="black")
        state_ax = axes[0]
        for seg in segments:
            indices = seg["indices"]
            if not indices:
                continue
            start_x = rows[indices[0]]["sample_index"]
            end_x = rows[indices[-1]]["sample_index"]
            label = seg["label"] % len(cluster_hatches)
            state_ax.add_patch(Rectangle(
                (start_x, 0.12), max(1, end_x - start_x), 0.76,
                facecolor="white", edgecolor="black", linewidth=0.65, hatch=cluster_hatches[label],
            ))
        if rows:
            state_ax.set_xlim(rows[0]["sample_index"], rows[-1]["sample_index"])
        state_ax.set_ylim(0, 1)
        state_ax.set_yticks([])
        state_ax.set_ylabel("聚类组编号")
        state_ax.grid(False)
        state_ax.spines["left"].set_visible(False)
        state_ax.spines["right"].set_visible(False)
        state_ax.spines["top"].set_visible(False)
        cluster_handles = [Patch(facecolor="white", edgecolor="black", hatch=cluster_hatches[label],
                                 label=f"组 {label}") for label in range(len(cluster_hatches))]
        state_ax.legend(handles=cluster_handles, loc="upper center", bbox_to_anchor=(0.5, 1.48),
                        ncol=5, frameon=False, title="K-Means++")
        for panel, field in enumerate(panel_channels):
            ax = axes[panel + 1]
            draw_channel(ax, field)
            sx, sy = selected_xy(selected, field)
            ax.scatter(sx, sy, s=5, facecolor="white", edgecolor="black", linewidth=0.5,
                       zorder=4, label="保留记录")
            ax.set_ylabel(field)
            style_axes(ax)
            ax.margins(x=0.01, y=0.08)
            if panel == 0:
                ax.legend(handles=[
                    Line2D([0], [0], color="black", lw=1.0, label="原始记录"),
                    Line2D([0], [0], marker="o", color="black", markerfacecolor="white",
                           markeredgecolor="black", markersize=4.5, linestyle="None", label="保留记录"),
                ], loc="upper left", frameon=True, facecolor="white", edgecolor="black", ncol=2)
        axes[-1].set_xlabel("原始记录序号")
        fig.tight_layout(rect=[0.04, 0.04, 1, 0.88], h_pad=0.9)
        path = PLOTS / "cluster_segment_sampling.png"
        finish_figure(fig, path)

    # 5. 黑白存储空间图：使用填充纹理区分数据格式。
    available = [row for row in storage_results if row.get("status") == "available" and row.get("bytes")]
    source_size = RAW_FILE.stat().st_size
    size_hatches = {"CSV": "", "Gzip CSV": "xx", "Parquet": "\\\\"}
    size_entries = [("原始输入 CSV", source_size, "//")]
    short_format_names = {"CSV": "CSV", "Gzip CSV": "Gzip CSV", "Parquet": "Parquet"}
    for row in available:
        size_entries.append((short_format_names.get(row["format"], row["format"]), int(row["bytes"]),
                             size_hatches.get(row["format"], "")))
    fig, ax = plt.subplots(figsize=(10, 5.8))
    positions = list(range(len(size_entries)))
    bars = ax.barh(positions, [entry[1] / 1024 for entry in size_entries],
                   color="white", edgecolor="black", linewidth=0.8, height=0.62)
    for bar, (_, _, hatch) in zip(bars, size_entries):
        bar.set_hatch(hatch)
    ax.set_yticks(positions, [entry[0] for entry in size_entries])
    ax.invert_yaxis()
    ax.set_xlabel("文件大小（KiB）")
    ax.set_title("图5 存储空间对比图", fontsize=15, fontweight="bold", pad=12, color="black")
    style_axes(ax)
    max_kib = max((entry[1] / 1024 for entry in size_entries), default=1)
    ax.set_xlim(0, max_kib * 1.55)
    for bar, (label, size, _) in zip(bars, size_entries):
        suffix = f"{size / 1024:.1f} KiB"
        ax.text(bar.get_width() + max_kib * 0.025, bar.get_y() + bar.get_height() / 2,
                suffix, va="center", fontsize=9, color="black")
    fig.tight_layout(rect=[0.02, 0.04, 1, 0.96])
    path = PLOTS / "storage_sizes.png"
    finish_figure(fig, path)

    # 6. 黑白读写耗时图：实心柱表示写入，斜线纹理表示读取。
    timed = [row for row in available if row.get("write_median_ms") not in (None, "")
             and row.get("read_median_ms") not in (None, "")]
    if timed:
        labels = [short_format_names.get(row["format"], row["format"]) for row in timed]
        write_times = [float(row["write_median_ms"]) for row in timed]
        read_times = [float(row["read_median_ms"]) for row in timed]
        fig, ax = plt.subplots(figsize=(10, 5.8))
        x_positions = list(range(len(labels)))
        bar_width = 0.34
        writes = ax.bar([x - bar_width / 2 for x in x_positions], write_times, width=bar_width,
                        color="black", edgecolor="black", label="写入")
        reads = ax.bar([x + bar_width / 2 for x in x_positions], read_times, width=bar_width,
                       color="white", edgecolor="black", hatch="///", label="读取")
        ax.set_xticks(x_positions, labels)
        ax.set_ylabel("耗时（毫秒）")
        ax.set_title("图6 存储格式读写耗时对比图", fontsize=15, fontweight="bold", pad=12, color="black")
        ax.legend(frameon=True, facecolor="white", edgecolor="black", ncol=2, loc="upper right")
        style_axes(ax)
        max_time = max(write_times + read_times, default=1)
        ax.set_ylim(0, max_time * 1.22)
        for bars in (writes, reads):
            for bar in bars:
                ax.annotate(f"{bar.get_height():.2f}",
                            (bar.get_x() + bar.get_width() / 2, bar.get_height()),
                            xytext=(0, 3), textcoords="offset points", ha="center", va="bottom",
                            fontsize=8, color="black")
        fig.tight_layout(rect=[0.02, 0.04, 1, 0.96])
        path = PLOTS / "storage_read_write_times.png"
        finish_figure(fig, path)
    return made


def write_code_explanation(quality, active_channels, sampling_results, storage_results, plot_paths, orig_entropy,
                           orig_mi):

    guide = [
        "# 代码与实验结果说明", "",
        "## 一、这段程序要解决什么问题", "",
        "程序以公开的 Starship 发射直播遥测 CSV 为输入，完成数据质量核查、信息论分析、K-Means++ 聚类分段抽样，再比较清洗后数据的文件大小和读写耗时。",
        "课设重点是减少冗余记录、尽量保留遥测曲线变化，并评估不同存储格式。",
        "",
        "## 二、数据是什么", "",
        f"- 输入记录数：{quality['source_rows']:,} 条；输入文件大小：{quality['input_file_bytes']:,} 字节。",
        f"- 数据字段：{', '.join(quality['source_columns'])}。其中本次可用于分析的信号字段为 {', '.join(active_channels) if active_channels else '无'}；全空信号字段为 {', '.join(quality['fully_missing_columns']) if quality['fully_missing_columns'] else '无'}。",
        f"- 清洗后保留 {quality['clean_rows']:,} 条含时间或信号值的记录；其中 {quality['rows_with_at_least_one_signal']:,} 条至少含一个有效信号。",
        "- 数据来自公开直播画面遥测区域的 OCR 识别结果，不是飞船官方机载原始遥测；可能含有识别误差。",
        "",
        "## 三、信息论分析与聚类分段", "",
        f"- **原始信息熵**：v1 的熵为 {orig_entropy.get('v1', 0):.3f} 比特，h1 的熵为 {orig_entropy.get('h1', 0):.3f} 比特。熵越大，说明该信号取值越分散、信息量越大。",
        f"- **互信息**：v1 与 h1 的互信息为 {orig_mi:.3f} 比特。互信息较高说明两个信号冗余度高；较低则说明各自携带独立信息。",
        "- **K-Means++ 数值聚类分组**：对标准化后的 (v1, h1) 特征进行聚类，划分为 5 个数值聚类组；组编号是算法标签，不代表真实飞行阶段。",
        "- **数值聚类组分段固定抽样**：按时间顺序把连续相同聚类组编号合并成片段，在片段内按固定步长抽样并保留边界点；效果以本数据的误差和保留比例为准。",
        "",
        "## 四、指标与结果应该怎么看", "",
        "- **保留比例**：抽样后留下的数据量占含有效信号记录数的比例；越低，数据量缩减越明显。",
        "- **最大归一化重建误差**：抽样后曲线重建时单个点的最大偏差占信号全局取值范围的比例；越低表示最差位置的曲线失真越小。",
        "- **KL 散度**：计算原始分布到抽样分布的 KL(P原始 || Q抽样)，采用原始信号范围内的固定等宽箱，并给每箱加入 0.5 个平滑计数；越低表示这两个经验分布越接近。",
        "- **熵比值**：原始数据和抽样数据共用原始范围内的 30 个等宽箱。它是描述性比值，可能超过 100%，不应单独解释为严格的信息保留百分比。",
        "",
        "### 本次抽样结果", "",
        "| 抽样方法 | 保留记录数 | 保留比例 | 最大归一化误差 | 归一化 RMSE | KL(v1) | 熵比值(v1) |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in sampling_results:
        guide.append(
            f"| {row['label']} | {row['sample_rows']:,} | {row['retention_pct']:.2f}% | "
            f"{row['max_error_pct_of_channel_range']:.3f}% | {row['rmse_pct_of_channel_range']:.3f}% | "
            f"{row['kl_v1']:.4f} | {row['entropy_ratio_v1']:.2f}% |"
        )
    guide.extend([
        "",
        "阅读表格时，先比较保留比例，再看最大重建误差、RMSE、KL 散度和熵比值。聚类方案按数值聚类组变化边界分段；是否优于其他方案，应以本次数据的具体指标判断。",
        "",
        "### 本次存储结果", "",
        "| 存储格式 | 状态 | 文件大小（字节） | 写入耗时中位数（毫秒） | 读取耗时中位数（毫秒） |",
        "|---|---|---:|---:|---:|",
    ])
    for row in storage_results:
        guide.append(
            f"| {storage_name_zh(row['format'])} | {'可用' if row['status'] == 'available' else '未生成（缺少依赖）'} | "
            f"{row['bytes'] if row['bytes'] != '' else '—'} | "
            f"{row['write_median_ms'] if row['write_median_ms'] != '' else '—'} | "
            f"{row['read_median_ms'] if row['read_median_ms'] != '' else '—'} |"
        )
    guide.extend([
        "",
        "## 五、输出文件分别是什么", "",
        "- `results/report.md`：课程设计实验报告。",
        "- `results/代码与结果说明.md`：本文件。",
        "- `results/sampling_comparison.csv`：包含信息论指标和各类抽样方案的对比。",
        "- `results/plots/telemetry_adaptive_1pct.png`：原始信号与 1% 误差阈值自适应抽样点的对照图。",
        "- `results/plots/sampling_tradeoff.png`：固定、自适应、数值聚类组三类方法的抽样量与最大重建误差权衡图；三个面板使用相同坐标范围。",
        "- `results/plots/kmeans_cluster_scatter.png`：v1 与 h1 的 K-Means++ 数值聚类组散点图；聚类组编号不代表真实飞行阶段。",
        "- `results/plots/cluster_segment_sampling.png`：数值聚类组时间分布带、信号曲线与抽样保留点的对应图。",
        "- `results/plots/storage_sizes.png`：不同存储格式的文件体积对比；原始输入仅作参照，其他格式使用清洗后数据。",
        "- `results/plots/storage_read_write_times.png`：CSV、Gzip CSV 与 Parquet 的读写耗时中位数对比。",
        "- `logs/experiment_run.json`：运行时间、环境、参数记录。",
        "",
        "## 六、运行方法", "",
        "在项目根目录打开终端，运行：", "",
        "```powershell",
        "python -m pip install -r requirements.txt",
        "python src/run_analysis.py",
        "```",
        "",
        "## 七、字体与结果清理", "",
        "- 出图用的中文字体随项目放在 `assets/fonts/`（Noto Sans SC 子集，SIL Open Font License 1.1），换机器或换系统都不用另外安装中文字体。",
        "- 字体选择顺序为：环境变量 `STARSHIP_TELEMETRY_FONT` → `assets/fonts/` → 系统中文字体；选中的字体先校验是否含图中用到的汉字，避免出现方块。",
        "- 每次运行结束会清理旧命名方案的抽样文件、已不在输出清单中的旧图表和 macOS 的 `._*` 元数据文件，保证结果目录只保留本次实验的产物。",
    ])
    (RESULTS / "代码与结果说明.md").write_text("\n".join(guide) + "\n", encoding="utf-8")


CORE_MODULES = (
    ("numpy", "numpy", "数值计算"),
    ("sklearn", "scikit-learn", "K-Means++ 聚类分段"),
    ("matplotlib", "matplotlib", "绘图"),
)
OPTIONAL_MODULES = (
    ("pyarrow", "pyarrow", "Parquet 存储对比"),
    ("fontTools", "fonttools", "中文字体字形校验"),
)


def _missing_modules(modules):
    missing = []
    for module, package, purpose in modules:
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append((package, purpose))
    return missing


def check_dependencies(allow_partial=False):
    """检查依赖；核心依赖缺失时直接退出，避免用不完整的结果覆盖上一次的正常结果。"""
    runnable = sys.executable or "python"
    print(f"Python 解释器：{runnable}（{platform.python_version()}）")
    missing_core = _missing_modules(CORE_MODULES)
    missing_optional = _missing_modules(OPTIONAL_MODULES)
    if missing_optional:
        names = "、".join(f"{package}（{purpose}）" for package, purpose in missing_optional)
        print(f"提示：缺少可选依赖：{names}，对应环节会被跳过。")
        print(f"      安装：{runnable} -m pip install -r requirements.txt")
    if missing_core:
        names = "、".join(f"{package}（{purpose}）" for package, purpose in missing_core)
        print(f"错误：当前解释器缺少核心依赖：{names}。")
        print(f"      安装依赖：{runnable} -m pip install -r requirements.txt")
        print("      或在 VS Code 中改用已装好依赖的解释器（命令行 Ctrl+Shift+P → Python: Select Interpreter）。")
        print("      如果确实只想跑不依赖这些库的部分，可加参数 --allow-partial（结果不完整，且不会清理旧输出）。")
        if not allow_partial:
            raise SystemExit(2)
    return [package for package, _ in missing_core + missing_optional]


def main():
    configure_console_utf8()
    missing_packages = check_dependencies(allow_partial="--allow-partial" in sys.argv[1:])
    if not RAW_FILE.exists():
        raise FileNotFoundError(f"找不到原始数据文件：{RAW_FILE}\n请将 telemetry-10fps.csv 放入 data/raw/ 目录后再运行。")
    PROCESSED.mkdir(parents=True, exist_ok=True)
    RESULTS.mkdir(parents=True, exist_ok=True)
    SAMPLED.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    fields, raw_records = load_raw(RAW_FILE)
    signal_fields = [field for field in SIGNAL_FIELDS if field in fields]
    active_channels = [field for field in signal_fields if any(row.get(field) is not None for row in raw_records)]
    dropped_fields = [field for field in signal_fields if field not in active_channels]
    clean_records = [row for row in raw_records if any(row.get(field) is not None for field in fields)]
    columns = ["sample_index", "t", *active_channels]
    clean_rows = [{key: row.get(key) for key in columns} for row in clean_records]
    quality = quality_summary(fields, raw_records, clean_rows, active_channels, dropped_fields)
    quality["input_sha256"] = hashlib.sha256(RAW_FILE.read_bytes()).hexdigest()
    quality["input_file_bytes"] = RAW_FILE.stat().st_size
    quality["source_link"] = "https://github.com/chrisjbillington/starship_telemetry"
    quality["source_method_note"] = "上游仓库说明，该数据由公开发射直播画面中的遥测区域经 OCR 识别得到；它不是飞船官方机载原始遥测数据。"
    (RESULTS / "data_quality.json").write_text(
        json.dumps(chinese_quality_summary(quality), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_csv(PROCESSED / "telemetry_clean.csv", clean_rows, columns)

    # ===== 信息论分析 =====
    v1_vals = [r["v1"] for r in clean_rows if r.get("v1") is not None]
    h1_vals = [r["h1"] for r in clean_rows if r.get("h1") is not None]
    orig_entropy = {"v1": calc_entropy(v1_vals), "h1": calc_entropy(h1_vals)}
    valid_idx = [i for i, r in enumerate(clean_rows) if r.get("v1") is not None and r.get("h1") is not None]
    orig_mi = calc_mutual_information(
        [clean_rows[i]["v1"] for i in valid_idx],
        [clean_rows[i]["h1"] for i in valid_idx]
    )

    # ===== 聚类分段 =====
    cluster_labels, segments = cluster_segments(clean_rows, active_channels[:2])

    schemes = []
    for step in (2, 5, 10, 20):
        schemes.append((f"fixed_step_{step}", f"固定间隔抽样（步长={step}）", "fixed", step, None))
    for epsilon_pct in (0.1, 0.5, 1.0, 2.0):
        slug = f"{epsilon_pct:g}".replace(".", "p")
        schemes.append(
            (f"adaptive_eps_{slug}pct", f"自适应抽样（误差阈值={epsilon_pct:g}%）", "adaptive", None, epsilon_pct / 100))
    if segments:
        for step in (2, 5, 10):
            schemes.append((f"cluster_step_{step}", f"数值聚类组分段固定抽样（步长={step}）", "cluster", step, None))

    observed_rows = sum(any(row.get(field) is not None for field in active_channels) for row in clean_rows)
    sample_sets = {}
    sampling_results = []
    for name, label, family, step, epsilon in schemes:
        if family == "fixed":
            selected = fixed_selection(clean_rows, active_channels, step)
        elif family == "adaptive":
            selected = adaptive_selection(clean_rows, active_channels, epsilon)
        else:
            selected = cluster_fixed_selection(clean_rows, active_channels, segments, step)

        selected_rows = [{key: clean_rows[index].get(key) for key in columns} for index in sorted(selected)]
        sample_sets[name] = selected
        output_path = SAMPLED / f"{name}.csv"
        write_csv(output_path, selected_rows, columns)
        gz_path = SAMPLED / f"{name}.csv.gz"
        write_gzip_csv(gz_path, selected_rows, columns)
        metrics = sampling_metrics(clean_rows, active_channels, selected, orig_entropy)
        sampling_results.append({
            "scheme": name, "label": label, "family": family,
            "step": step if step is not None else "",
            "epsilon_pct": epsilon * 100 if epsilon is not None else "",
            "sample_rows": len(selected), "observed_signal_rows": observed_rows,
            "retention_pct": len(selected) / observed_rows * 100 if observed_rows else 0.0,
            "csv_bytes": output_path.stat().st_size, "gzip_bytes": gz_path.stat().st_size,
            **metrics,
        })

    sampling_output = chinese_sampling_rows(sampling_results)
    sampling_columns = list(sampling_output[0].keys()) if sampling_output else []
    write_csv(RESULTS / "sampling_comparison.csv", sampling_output, sampling_columns, excel_friendly=True)

    storage_results = benchmark_storage(clean_rows, columns, RAW_FILE.stat().st_size)
    storage_output = chinese_storage_rows(storage_results)
    storage_columns = list(storage_output[0].keys()) if storage_output else []
    write_csv(RESULTS / "storage_comparison.csv", storage_output, storage_columns, excel_friendly=True)
    made_plots = make_pngs(clean_rows, active_channels, sample_sets, sampling_results, storage_results, cluster_labels,
                           segments)

    # ===== 报告生成 =====
    lowest_retention_cluster = min(
        (row for row in sampling_results if row["family"] == "cluster"),
        key=lambda row: row["retention_pct"], default=None,
    )

    report = [
        "# Starship 遥测数据预处理与存储实验", "",
        "## 数据核查与信息论分析",
        f"- 原始记录：{quality['source_rows']:,} 条；原始 CSV：{quality['input_file_bytes']:,} 字节。",
        f"- 删除全字段缺失记录：{quality['removed_fully_empty_rows']:,} 条；清洗后保留 {quality['clean_rows']:,} 条有时间或信号值的记录。",
        f"- 可用信号列：{', '.join(active_channels) if active_channels else '无'}；全空信号列：{', '.join(dropped_fields) if dropped_fields else '无'}。",
        f"- 原始 v1 熵：{orig_entropy['v1']:.3f} 比特；原始 h1 熵：{orig_entropy['h1']:.3f} 比特；互信息（v1;h1）：{orig_mi:.3f} 比特。",
        "- 不插补缺失值；全空信号列不进入抽样误差计算。保留 sample_index，使整秒重复时间仍按原始记录顺序参与重建。",
        "",
        "## 抽样与聚类方法",
        "- **固定间隔基线**：按全局有效记录顺序每隔 2、5、10、20 条取样，再补齐各信号连续有效片段的端点。",
        "- **误差约束自适应**：对每个信号的连续有效片段按线性重建误差递归细分；合并信号的保留点后再次检查公共样本集，补点至各信号均满足阈值。",
        "- **K-Means++ 数值聚类组分段**：对标准化后的 (v1, h1) 特征进行聚类，划分为 5 个数值聚类组，再按时间顺序合并连续的同组记录；组编号仅用于区分算法分组，不代表真实飞行阶段。每个片段内执行固定间隔抽样并保留边界点。",
        "", "## 抽样结果", "",
        "| 方法 | 保留记录 | 保留比例 | 最大归一化误差 | 归一化 RMSE | KL散度(v1) | 熵比值(v1) |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in sampling_results:
        report.append(
            f"| {row['label']} | {row['sample_rows']:,} | {row['retention_pct']:.2f}% | "
            f"{row['max_error_pct_of_channel_range']:.3f}% | {row['rmse_pct_of_channel_range']:.3f}% | "
            f"{row['kl_v1']:.4f} | {row['entropy_ratio_v1']:.2f}% |"
        )

    report.extend([
        "",
        "## 结果怎么理解",
        "- 数值聚类组分段抽样按连续同类片段分别取样并保留边界；聚类组编号仅用于区分算法形成的数据组，不直接代表真实飞行阶段。",
        "- KL 散度和熵比值用于比较抽样前后的经验分布；应与重建误差、保留比例结合解读，不把单项指标当作综合优劣结论。",
    ])
    if lowest_retention_cluster:
        report.append(
            f"- 聚类方案中，按保留比例最低选出的方案（步长={lowest_retention_cluster['step']}）保留 "
            f"{lowest_retention_cluster['sample_rows']:,} 条（{lowest_retention_cluster['retention_pct']:.2f}%），"
            f"最大归一化误差为 {lowest_retention_cluster['max_error_pct_of_channel_range']:.3f}%，"
            f"KL 散度(v1) 为 {lowest_retention_cluster['kl_v1']:.4f}。该选择只按保留比例确定，不代表综合最优。"
        )

    report.extend([
        "", "## 存储格式结果", "",
        "| 格式 | 状态 | 文件大小（字节） | 写入耗时中位数（毫秒） | 读取耗时中位数（毫秒） |",
        "|---|---|---:|---:|---:|",
    ])
    for row in storage_results:
        report.append(
            f"| {storage_name_zh(row['format'])} | {'可用' if row['status'] == 'available' else '未生成（缺少依赖）'} | "
            f"{row['bytes'] if row['bytes'] != '' else '—'} | "
            f"{row['write_median_ms'] if row['write_median_ms'] != '' else '—'} | "
            f"{row['read_median_ms'] if row['read_median_ms'] != '' else '—'} |"
        )
    report.extend([
        "",
        "## 数据来源与边界", "",
        "数据来自 chrisjbillington/starship_telemetry 仓库的 telemetry-10fps.csv。该仓库说明，数据由 SpaceX 发射直播画面裁剪遥测区域后经 OCR 识别得到，存在识别误差；它不是 SpaceX 官方原始遥测。CSV 未提供充分的单位说明，因此本报告保留字段名，不猜测单位。",
        "",
        "## 复现", "",
        "    python -m pip install -r requirements.txt",
        "    python src/run_analysis.py",
        "",
        "图表位于 results/plots/（含抽样示意、方法权衡、聚类分段和存储空间/耗时对比）；信息论与抽样指标位于 results/sampling_comparison.csv。",
    ])
    (RESULTS / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    write_code_explanation(quality, active_channels, sampling_results, storage_results, made_plots, orig_entropy,
                           orig_mi)

    run_log = {
        "运行时间（UTC）": datetime.now(timezone.utc).isoformat(),
        "Python版本": sys.version.split()[0],
        "运行平台": platform.platform(),
        "输入文件SHA256校验值": quality["input_sha256"],
        "实验参数": {
            "固定间隔抽样步长": [2, 5, 10, 20],
            "自适应误差阈值（占信号范围百分比）": [0.1, 0.5, 1.0, 2.0],
            "聚类方法": "K-Means++ (K=5)",
            "存储基准重复次数": REPEATS,
        },
        "生成结果": {
            "清洗后记录数": len(clean_rows),
            "抽样方案数": len(sampling_results),
            "图表文件": [str(path.relative_to(ROOT)) for path in made_plots],
            "缺失依赖": missing_packages,
        },
    }
    (LOGS / "experiment_run.json").write_text(json.dumps(run_log, ensure_ascii=False, indent=2), encoding="utf-8")

    if missing_packages:
        print("本次运行缺少依赖，结果不完整，已跳过清理：上一次生成的图表和抽样文件保持不动。")
    else:
        try:
            prune_stale_outputs({row["scheme"] for row in sampling_results}, {path.stem for path in made_plots})
        except OSError as error:
            # 清理属于收尾工作，失败时不应把一次成功的实验变成“运行失败”。
            print(f"提示：清理过期输出时出错（{error}），本次结果不受影响，可手动检查 results/ 目录。")

    print("分析完成。")
    print(f"原始记录：{quality['source_rows']:,} 条；清洗后记录：{len(clean_rows):,} 条。")
    print(f"原始熵：v1={orig_entropy['v1']:.3f} 比特，h1={orig_entropy['h1']:.3f} 比特；互信息={orig_mi:.3f} 比特。")
    print("\n抽样对比（保留记录数 / 保留比例 / 最大归一化误差 / KL散度）：")
    for row in sampling_results:
        print(
            f"- {row['label']}：{row['sample_rows']:,} 条 / {row['retention_pct']:.2f}% / "
            f"{row['max_error_pct_of_channel_range']:.3f}% / KL散度={row['kl_v1']:.4f}"
        )
    print("\n存储格式对比：")
    for row in storage_results:
        status = "可用" if row["status"] == "available" else "未生成（缺少依赖）"
        size = f"{row['bytes']:,} 字节" if row["bytes"] != "" else "—"
        print(f"- {storage_name_zh(row['format'])}：{status}，{size}。")
    print("\n结果报告：results/report.md")
    print("代码与结果说明：results/代码与结果说明.md")
    if made_plots:
        print("图表：" + "、".join(str(path.relative_to(ROOT)) for path in made_plots))
    elif "matplotlib" in missing_packages:
        print("图表：未生成（当前解释器缺少 matplotlib，见上方安装提示；中文字体已随项目自带）")
    else:
        print("图表：未生成（原因见上方提示）")
    if missing_packages:
        print("\n提示：本次缺少依赖，结果不完整，请按上面的命令安装后重跑。")


if __name__ == "__main__":
    main()
