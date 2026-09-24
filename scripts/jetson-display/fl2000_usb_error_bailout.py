#!/usr/bin/env python3
"""Патч драйвера fl2000 (DKMS, /usr/src/fl2000_drm-1.0): не давать шторму USB-ошибок убивать систему.

19.09.2026: при деградации USB 3.0-линка донгла (FL2000 через «проход» хаба) каждый bulk-URB
завершался с -71 (EPROTO). Драйвер на КАЖДОЕ завершение печатал dev_err в консоль ядра и тут же
пересобирал следующий URB (memcpy 2.7 МБ + submit): ~430 ошибок/с, ~1.2 ГБ/с memcpy и сотни
строк/с в последовательную консоль 115200. Userspace переставал планироваться, Jetson «умирал»
через ~6 минут после запуска стека, экраны при этом горели (USB-прерывания ещё жили).

Правки (идемпотентные, оригиналы сохраняются как *.orig-20260919):
  1. fl2000.h: dev_err -> dev_err_ratelimited в fl2000_urb_status().
  2. fl2000_streaming.c: считать подряд идущие ошибки завершения; после
     FL2000_MAX_URB_ERRORS подряд остановить поток (stream->enabled = false) и один раз
     сообщить. Картинка пропадёт до переподключения донгла/ребута, зато система живёт.

Запуск на Jetson: sudo python3 fl2000_usb_error_bailout.py && sudo dkms build fl2000_drm/1.0
&& sudo dkms install --force fl2000_drm/1.0; вступает в силу после ребута (живьём драйвер
не перезагружается).
"""

import shutil
import sys
from pathlib import Path

SRC = Path("/usr/src/fl2000_drm-1.0")
SUFFIX = ".orig-20260919"


def backup(p: Path) -> None:
    b = p.with_name(p.name + SUFFIX)
    if not b.exists():
        shutil.copy2(p, b)


def patch_header() -> bool:
    p = SRC / "fl2000.h"
    s = p.read_text()
    old = '\t\tdev_err(&usb_dev->dev, "Nonzero urb status, %d\\n", status);'
    new = '\t\tdev_err_ratelimited(&usb_dev->dev, "Nonzero urb status, %d\\n", status);'
    if new in s:
        return False
    if old not in s:
        sys.exit("fl2000.h: не нашёл строку dev_err в fl2000_urb_status()")
    backup(p)
    p.write_text(s.replace(old, new))
    return True


def patch_streaming() -> bool:
    p = SRC / "fl2000_streaming.c"
    s = p.read_text()
    if "FL2000_MAX_URB_ERRORS" in s:
        return False

    # 1. поле-счётчик в struct fl2000_stream (после bytes_pix)
    old_field = "\tu32 bytes_pix;\n"
    if old_field not in s:
        sys.exit("fl2000_streaming.c: не нашёл поле bytes_pix в struct fl2000_stream")
    s = s.replace(
        old_field,
        old_field + "\t/* Подряд идущие сбойные завершения bulk-URB; см. FL2000_MAX_URB_ERRORS */\n"
        "\tunsigned int urb_errors;\n",
        1,
    )

    # 2. константа рядом с FL2000_URB_TIMEOUT
    old_def = "#define FL2000_URB_TIMEOUT 100\n"
    if old_def not in s:
        sys.exit("fl2000_streaming.c: не нашёл FL2000_URB_TIMEOUT")
    s = s.replace(
        old_def,
        old_def + "\n/* Столько подряд сбойных завершений (EPROTO и т.п.) -- линк мёртв: останавливаем\n"
        " * поток вместо бесконечного resubmit. Иначе ~430 ошибок/с + memcpy кадра на каждую +\n"
        " * dev_err в консоль ядра, и userspace перестаёт планироваться (19.09.2026).\n"
        " */\n#define FL2000_MAX_URB_ERRORS 64\n",
        1,
    )

    # 3. учёт в completion: до kick'а workqueue
    old_cb = (
        "\t\tdrm_crtc_handle_vblank(stream->crtc);\n\n"
        "\t\t/* Kick transmit workqueue */\n"
        "\t\tup(&stream->work_sem);\n\n"
        "\t\tfl2000_urb_status(usb_dev, urb->status, urb->pipe);\n"
    )
    new_cb = (
        "\t\tdrm_crtc_handle_vblank(stream->crtc);\n\n"
        "\t\tif (urb->status == 0) {\n"
        "\t\t\tstream->urb_errors = 0;\n"
        "\t\t} else if (urb->status != -ECONNRESET && urb->status != -ENOENT &&\n"
        "\t\t\t   urb->status != -ESHUTDOWN) {\n"
        "\t\t\tif (++stream->urb_errors == FL2000_MAX_URB_ERRORS) {\n"
        "\t\t\t\tdev_err(&usb_dev->dev,\n"
        "\t\t\t\t\t\"%u consecutive URB errors (last %d): USB link is dead, \"\n"
        "\t\t\t\t\t\"stopping the stream; replug the dongle or reboot\\n\",\n"
        "\t\t\t\t\tstream->urb_errors, urb->status);\n"
        "\t\t\t\tstream->enabled = false;\n"
        "\t\t\t}\n"
        "\t\t}\n\n"
        "\t\t/* Kick transmit workqueue (it exits on its own once enabled == false) */\n"
        "\t\tup(&stream->work_sem);\n\n"
        "\t\tif (stream->enabled)\n"
        "\t\t\tfl2000_urb_status(usb_dev, urb->status, urb->pipe);\n"
    )
    if old_cb not in s:
        sys.exit("fl2000_streaming.c: completion-обработчик выглядит иначе, чем ожидалось")
    s = s.replace(old_cb, new_cb, 1)

    backup(p)
    p.write_text(s)
    return True


def patch_drm_release() -> bool:
    """23.09.2026: обрыв USB-линка = disconnect + мгновенный re-probe. Новый bind падает на
    component_bind_all() с -EBUSY (мост it66121 ещё принадлежит старому DRM-устройству, которое
    держит weston), devres откатывает probe -> drm_put_dev() -> .release -> drm_atomic_helper_shutdown()
    на устройстве, которое никогда не регистрировалось: refcount 0, у CRTC нет state ->
    NULL pointer dereference -> (с panic_on_oops=1) паника и ребут робота.
    Плюс ручной drm_mode_config_cleanup() при drmm_mode_config_init() -- двойная очистка.
    Плюс TODO upstream: при ошибке bind созданные stream/intr не уничтожаются."""
    p = SRC / "fl2000_drm.c"
    s = p.read_text()
    if "drm->registered" in s:
        return False

    old_rel = (
        "static void fl2000_drm_release(struct drm_device *drm)\n"
        "{\n"
        "\tdrm_atomic_helper_shutdown(drm);\n"
        "\tdrm_mode_config_cleanup(drm);\n"
        "}\n"
    )
    new_rel = (
        "static void fl2000_drm_release(struct drm_device *drm)\n"
        "{\n"
        "\t/* Только зарегистрированное устройство (bind дошёл до drm_dev_register) имеет\n"
        "\t * состояния CRTC/plane, которые есть что гасить. После обрыва USB-линка новый\n"
        "\t * bind падает с -EBUSY (мост ещё у старого устройства), devres зовёт release на\n"
        "\t * устройстве без состояний, и disable_all() разыменовывает NULL (23.09.2026).\n"
        "\t * mode_config создан drmm_mode_config_init() и чистится managed-путём сам.\n"
        "\t */\n"
        "\tif (drm->registered)\n"
        "\t\tdrm_atomic_helper_shutdown(drm);\n"
        "}\n"
    )
    if old_rel not in s:
        sys.exit("fl2000_drm.c: fl2000_drm_release() выглядит иначе, чем ожидалось")
    s = s.replace(old_rel, new_rel, 1)

    # откат stream/intr при ошибке bind после их создания
    old_bind_err = (
        "\tret = component_bind_all(master, &drm_if->pipe);\n"
        "\tif (ret) {\n"
        "\t\tdev_err(drm->dev, \"Cannot attach bridge (%d)\", ret);\n"
        "\t\treturn ret;\n"
        "\t}\n"
    )
    new_bind_err = (
        "\tret = component_bind_all(master, &drm_if->pipe);\n"
        "\tif (ret) {\n"
        "\t\tdev_err(drm->dev, \"Cannot attach bridge (%d)\", ret);\n"
        "\t\tgoto err_destroy;\n"
        "\t}\n"
    )
    if old_bind_err not in s:
        sys.exit("fl2000_drm.c: блок component_bind_all() выглядит иначе, чем ожидалось")
    s = s.replace(old_bind_err, new_bind_err, 1)

    for old in (
        "\t\tdev_err(drm->dev, \"Failed to initialize %d VBLANK(s) (%d)\",\n"
        "\t\t\tdrm->mode_config.num_crtc, ret);\n"
        "\t\treturn ret;\n",
        "\t\tdev_err(drm->dev, \"Cannot register DRM device (%d)\", ret);\n"
        "\t\treturn ret;\n",
    ):
        if old not in s:
            sys.exit("fl2000_drm.c: блок ошибки в bind выглядит иначе, чем ожидалось")
        s = s.replace(old, old.replace("\t\treturn ret;\n", "\t\tgoto err_destroy;\n"), 1)

    old_tail = (
        "\t * a connector that has not actually changed.  Nothing here needs /dev/fb1: the console is\n"
        "\t * off (fbcon=map:0) and weston drives the card through DRM directly.\n"
        "\t */\n"
        "\n"
        "\treturn 0;\n"
        "}\n"
    )
    new_tail = (
        "\t * a connector that has not actually changed.  Nothing here needs /dev/fb1: the console is\n"
        "\t * off (fbcon=map:0) and weston drives the card through DRM directly.\n"
        "\t */\n"
        "\n"
        "\treturn 0;\n"
        "\n"
        "err_destroy:\n"
        "\t/* Иначе интерруптный URB и workqueue потока переживают провалившийся bind и лезут\n"
        "\t * в уже освобождённый DRM (upstream TODO \"release on errors\"). */\n"
        "\tfl2000_intr_destroy(usb_dev);\n"
        "\tfl2000_stream_destroy(usb_dev);\n"
        "\treturn ret;\n"
        "}\n"
    )
    if old_tail not in s:
        sys.exit("fl2000_drm.c: хвост fl2000_drm_bind() выглядит иначе, чем ожидалось")
    s = s.replace(old_tail, new_tail, 1)

    backup(p)
    p.write_text(s)
    return True


def patch_stream_disable_deadlock() -> bool:
    """23.09.2026: после обрыва линка fl2000_stream_disable() делает enabled=false и
    drain_workqueue(), а fl2000_stream_work() спит в down_interruptible(&work_sem), которую
    будят только completion'ы URB -- их больше нет. Воркер не просыпается, drain не
    возвращается, commit-work DRM висит, weston при закрытии /dev/dri уходит в D-state навсегда
    (flush_work в drm_release), экран не восстановить без ребута, а systemd не может
    перезапустить weston. Лечение: будить воркер при disable и ждать семафор с таймаутом,
    перепроверяя enabled."""
    p = SRC / "fl2000_streaming.c"
    s = p.read_text()
    if "down_timeout(&stream->work_sem" in s:
        return False

    old_wait = (
        "\twhile (stream->enabled) {\n"
        "\t\tret = down_interruptible(&stream->work_sem);\n"
        "\t\tif (ret) {\n"
        "\t\t\tdev_err(&usb_dev->dev, \"Work interrupt error %d\", ret);\n"
        "\t\t\tstream->enabled = false;\n"
        "\t\t\treturn;\n"
        "\t\t}\n"
    )
    new_wait = (
        "\twhile (stream->enabled) {\n"
        "\t\t/* С таймаутом: completion'ы URB -- единственный источник up(), после обрыва USB\n"
        "\t\t * их нет, и воркер, спящий в down_interruptible(), никогда не выйдет, а\n"
        "\t\t * drain_workqueue() в fl2000_stream_disable() повиснет вместе с weston (23.09.2026).\n"
        "\t\t */\n"
        "\t\tret = down_timeout(&stream->work_sem, msecs_to_jiffies(200));\n"
        "\t\tif (ret == -ETIME)\n"
        "\t\t\tcontinue;\n"
        "\t\tif (ret) {\n"
        "\t\t\tdev_err(&usb_dev->dev, \"Work interrupt error %d\", ret);\n"
        "\t\t\tstream->enabled = false;\n"
        "\t\t\treturn;\n"
        "\t\t}\n"
        "\t\tif (!stream->enabled)\n"
        "\t\t\treturn;\n"
    )
    if old_wait not in s:
        sys.exit("fl2000_streaming.c: цикл ожидания в fl2000_stream_work() выглядит иначе")
    s = s.replace(old_wait, new_wait, 1)

    old_dis = (
        "\tstream->enabled = false;\n"
        "\n"
        "\tdrain_workqueue(stream->work_queue);\n"
    )
    new_dis = (
        "\tstream->enabled = false;\n"
        "\t/* Разбудить воркер, если он ждёт семафор: иначе drain ниже не вернётся. */\n"
        "\tup(&stream->work_sem);\n"
        "\n"
        "\tdrain_workqueue(stream->work_queue);\n"
    )
    if old_dis not in s:
        sys.exit("fl2000_streaming.c: fl2000_stream_disable() выглядит иначе")
    s = s.replace(old_dis, new_dis, 1)

    backup(p)
    p.write_text(s)
    return True


if __name__ == "__main__":
    h = patch_header()
    c = patch_streaming()
    d = patch_drm_release()
    e = patch_stream_disable_deadlock()
    print(f"deadlock fix: {'patched' if e else 'already patched'}")
    print(f"fl2000.h: {'patched' if h else 'already patched'}; "
          f"fl2000_streaming.c: {'patched' if c else 'already patched'}; "
          f"fl2000_drm.c: {'patched' if d else 'already patched'}")
