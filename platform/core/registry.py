"""品牌註冊表。掃描 brands/ 目錄自動載入，新增品牌不需要改這個檔案。"""
import importlib
import inspect
import pkgutil

from .brand import BaseBrand

_registry = {}


def load_brands(package="brands"):
    global _registry
    if _registry:
        return _registry
    pkg = importlib.import_module(package)
    for mod_info in pkgutil.iter_modules(pkg.__path__):
        module = importlib.import_module(f"{package}.{mod_info.name}")
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, BaseBrand) and obj is not BaseBrand and obj.slug:
                if obj.__module__ != module.__name__:
                    continue          # 只收在本模組定義的，跳過 import 進來的基底
                _registry[obj.slug] = obj
    return _registry


def get(slug):
    brands = load_brands()
    if slug not in brands:
        raise KeyError(f"未知品牌: {slug}（可用：{', '.join(sorted(brands))}）")
    return brands[slug]()


def all_slugs():
    return sorted(load_brands())


def summary():
    return [{"slug": s, "name": load_brands()[s].name,
             "collection": load_brands()[s].collection,
             "schedule": load_brands()[s].schedule}
            for s in all_slugs()]
