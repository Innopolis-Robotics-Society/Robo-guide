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


if __name__ == "__main__":
    h = patch_header()
    c = patch_streaming()
    print(f"fl2000.h: {'patched' if h else 'already patched'}; "
          f"fl2000_streaming.c: {'patched' if c else 'already patched'}")
