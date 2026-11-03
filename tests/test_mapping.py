# -*- coding: utf-8 -*-
"""ConsistencyMapper：一致性、冲突处理、作用域与内存安全边界。"""
from pathlib import Path

import pytest

from localdocmask.application.composition import build_mapper
from localdocmask.domain.exceptions import ConfigError, MappingCollisionError
from localdocmask.infrastructure.config.settings import Settings
from localdocmask.security.redaction.mapping import ConsistencyMapper


def test_same_entity_repeat_is_stable():
    m = ConsistencyMapper()
    a = m.replacement("公司名", "字节跳动")
    b = m.replacement("公司名", "字节跳动")
    assert a == b
    assert a != "字节跳动"


def test_cross_file_consistency_with_shared_mapper():
    # 同一批次共享同一 mapper 实例，模拟跨文件
    m = ConsistencyMapper(scope="batch")
    # 文件1
    v1 = m.replacement("公司名", "字节跳动")
    p1 = m.replacement("姓名", "张三")
    # 文件2（同一 mapper）
    v2 = m.replacement("公司名", "字节跳动")
    p2 = m.replacement("姓名", "张三")
    assert v1 == v2
    assert p1 == p2


def test_different_entities_no_collision_even_beyond_pool():
    m = ConsistencyMapper()
    # 公司池仅 10 个，构造 15 个真实公司 → 确定性扩展，全部唯一、不抛错
    companies = [f"真实公司{i}" for i in range(15)]
    company_values = [m.replacement("公司名", c) for c in companies]
    assert len(set(company_values)) == 15
    # 姓名池组合远超默认循环数，构造 60 个真实姓名 → 全部唯一
    persons = [f"真实姓名{i}" for i in range(60)]
    person_values = [m.replacement("姓名", p) for p in persons]
    assert len(set(person_values)) == 60
    # 公司与姓名两个命名空间也不互相占用
    assert set(company_values).isdisjoint(set(person_values))


def test_scope_task_is_instance_local():
    m = ConsistencyMapper(scope="task")
    a = m.replacement("姓名", "张三")
    assert m.replacement("姓名", "张三") == a


def test_global_scope_requires_persistence():
    with pytest.raises(ConfigError):
        ConsistencyMapper(scope="global", persistence="memory")


def test_memory_persistence_still_works_without_file(tmp_path, monkeypatch):
    def forbidden_run(*args, **kwargs):
        raise AssertionError("memory 模式不得调用 subprocess/icacls")

    monkeypatch.setattr("subprocess.run", forbidden_run)
    store = tmp_path / "mapping"
    m = ConsistencyMapper(scope="batch", persistence="memory", store_dir=store)

    company_a = m.replacement("公司名", "云启科技")
    company_b = m.replacement("公司名", "星轨数据")
    person_a = m.replacement("姓名", "张三")
    person_b = m.replacement("姓名", "李四")

    assert company_a == m.replacement("公司名", "云启科技")
    assert person_a == m.replacement("姓名", "张三")
    assert len({company_a, company_b}) == 2
    assert len({person_a, person_b}) == 2
    assert not store.exists()
    assert not (store / "mapping.protected.json").exists()


def test_disk_mapping_persistence_is_rejected_by_settings_validation():
    settings = Settings()
    settings.masking.mapping_persistence = "disk"
    with pytest.raises(ConfigError) as caught:
        settings._validate()
    message = str(caught.value)
    assert "disk" in message
    assert "明文映射" in message
    assert "DPAPI" in message


def test_build_mapper_rejects_disk_even_when_validation_is_bypassed():
    settings = Settings()
    settings.masking.mapping_persistence = "disk"
    with pytest.raises(ConfigError):
        build_mapper(settings)


def test_protected_file_persistence_is_rejected_without_side_effects(tmp_path):
    store = tmp_path / "mapping"
    with pytest.raises(ConfigError):
        ConsistencyMapper(
            scope="batch",
            persistence="protected_file",
            store_dir=store,
        )
    assert not store.exists()
    assert not (store / "mapping.protected.json").exists()


def test_mapping_collision_when_unallocatable():
    m = ConsistencyMapper()
    m._used.add("固定虚构值")
    # 让候选发生器永远返回同一个已占用值 → 应抛 MappingCollisionError
    m._candidate = lambda kind, n: "固定虚构值"
    with pytest.raises(MappingCollisionError):
        m.replacement("公司名", "某公司")


def test_repr_does_not_leak_mapping():
    m = ConsistencyMapper()
    m.replacement("公司名", "字节跳动")
    r = repr(m)
    assert "字节跳动" not in r
    assert "mappings" not in r.lower()
