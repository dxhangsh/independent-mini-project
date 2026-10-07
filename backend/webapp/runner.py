# -*- coding: utf-8 -*-
"""流水线执行器：单目录扫描与批量 worker 共用的执行入口。"""
from engine.pipeline import run_folder
from webapp.state import plan_to_dict


def run_folder_pipeline(serial, folder, product_word=None, progress=None, on_image=None):
    """跑完一个目录，返回 (plan_dict, review_count, total)。

    progress/on_image 与 engine.pipeline.run_folder 同义；progress 缺省为静默。
    """
    d, review, total = None, 0, 0
    plan = run_folder(serial, folder,
                      product_word=product_word or None,
                      progress=progress or (lambda *_: None),
                      on_image=on_image)
    d = plan_to_dict(plan)
    return d, plan.review_count, len(plan.rows)
