"""图片解密的失败路径回归：不合法的输入必须报错，不能静默写出坏图。"""

import struct
import sys
import tempfile
import unittest
from pathlib import Path

from Crypto.Cipher import AES

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wechat_mcp import images


AES_KEY = b"0123456789abcdef"


def build_v2(plain: bytes, tail_plain: bytes, xor_key: int) -> bytes:
    """按微信 4.x V2 容器格式构造一个可解密样本（aes_size 固定为 32）。"""
    assert len(plain) == 32
    pad = 16  # PKCS7：32 已对齐，仍需补一整块
    cipher = AES.new(AES_KEY, AES.MODE_ECB).encrypt(plain + bytes([pad]) * pad)
    tail = bytes([b ^ xor_key for b in tail_plain])
    return images._V2_MAGIC_FULL + struct.pack("<II", 32, len(tail_plain)) + b"\x00" + cipher + tail


class TestImageDecryptFailures(unittest.TestCase):
    def test_non_jpeg_without_thumbnail_is_rejected_instead_of_corrupted(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            png_plain = b"\x89PNG\r\n\x1a\n" + b"A" * 24
            (root / "a.dat").write_bytes(build_v2(png_plain, b"TAIL", 0x5A))
            data, fmt, err = images.decrypt_image_dat(root / "a.dat", aes_key=AES_KEY)
        self.assertIsNone(data)
        self.assertEqual(fmt, "")
        self.assertIn("Cannot derive the global XOR key", err)
        # 失败推导不能被永久缓存
        self.assertNotIn(str(root), images._XOR_KEY_CACHE)

    def test_thumbnail_added_later_is_picked_up_after_failed_attempt(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            jpeg_plain = b"\xff\xd8\xff" + b"J" * 29
            tail_plain = b"TAIL\xd9"
            key = 0x5A
            (root / "a_t.dat").write_bytes(build_v2(jpeg_plain, tail_plain, key))
            png_plain = b"\x89PNG\r\n\x1a\n" + b"P" * 24
            (root / "a.dat").write_bytes(build_v2(png_plain, tail_plain, key))
            data, fmt, err = images.decrypt_image_dat(root / "a.dat", aes_key=AES_KEY)
        self.assertIsNone(err)
        self.assertEqual(fmt, "png")
        self.assertEqual(data, png_plain + tail_plain)

    def test_wrong_aes_key_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            plain = b"\x89PNG\r\n\x1a\n" + b"P" * 24
            (root / "a.dat").write_bytes(build_v2(plain, b"TAIL", 0x5A))
            data, fmt, err = images.decrypt_image_dat(
                root / "a.dat", aes_key=b"fedcba9876543210"
            )
        self.assertIsNone(data)
        self.assertIn("AES decryption failed", err)

    def test_unrecognized_payload_is_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            # 先提供一个可推导 XOR 密钥的 JPEG 缩略图，让流程走到「识别文件头」这一步。
            (root / "thumb_t.dat").write_bytes(
                build_v2(b"\xff\xd8\xff" + b"J" * 29, b"TAIL\xd9", 0x5A)
            )
            garbage = b"\x01\x02\x03\x04" + b"G" * 28
            (root / "a.dat").write_bytes(build_v2(garbage, b"TAIL", 0x5A))
            data, fmt, err = images.decrypt_image_dat(root / "a.dat", aes_key=AES_KEY)
        self.assertIsNone(data)
        self.assertIn("not a recognizable image", err)


if __name__ == "__main__":
    unittest.main()
