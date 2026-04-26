# Milli Sozluk Gezgini

Turkce Wiktionary verisini kullanarak yerelde calisan, Streamlit tabanli bir sozluk denemesi.

Projenin ana calisan dosyasi `sozluk_pro.py` dosyasidir. Diger Python dosyalari daha eski denemeler, veri donusumleri veya yardimci calismalar olarak klasorde durur.

## Neler var?

- Kelime arama ekrani
- Basili sozluk hissi veren alfabetik fihrist
- Analitik paneller:
  - Koken dagilimi
  - Ozel ad / yerlesim / genel kelime orani
  - En cok anlami olan kelimeler
  - Ornek cumle kapsama orani
  - Bas harf dagilimi

## Teknoloji

- Python 3.10+
- Streamlit
- Pandas
- Altair
- SQLite
- Requests

## Kurulum

1. Sanal ortam olustur:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

2. Bagimliliklari yukle:

```powershell
pip install -r requirements.txt
```

3. Uygulamayi baslat:

```powershell
streamlit run sozluk_pro.py
```

## Veri

Uygulama iki farkli veri dosyasi ile calisabilir:

- `milli_sozluk_tr.db`: Uygulamanin kullandigi yerel SQLite veritabani
- `trwiktionary-latest-pages-articles.xml.bz2`: Gerekirse yeniden veritabani kurmak icin kullanilan dump dosyasi

Ilk calistirmada veritabani yoksa uygulama kurulum akisina gecer.

## Notlar

- Fihrist yalnizca Turk alfabesiyle sinirlandirilmistir.
- Buyuk veri dosyalari ve yerel veritabanlari repoya dahil edilmemelidir.
- Uygulama deneysel bir projedir; amac tam urunlestirme degil, veri ve arayuz fikirlerini denemektir.

## Dizin yapisi

Temel dosyalar:

- `sozluk_pro.py`: Ana uygulama
- `requirements.txt`: Python bagimliliklari
- `.gitignore`: Repoya alinmamasi gereken buyuk ve yerel dosyalar

Klasorde bulunan diger `.py` dosyalari aktif uygulama akisinin parcasi degildir.

## Lisans

Bu repo icin henuz ayri bir lisans tanimi eklenmedi.
