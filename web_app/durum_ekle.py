import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from isg_database import prepare_database

prepare_database()

print("Veritabanı şeması başarıyla hazırlandı")
