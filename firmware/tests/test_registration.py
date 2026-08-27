from firmware.src.registration import make_slug

def test_registration_slug_transliterates():
    assert make_slug('ГОСТ 123','ГОСТ','Электрика').startswith('GOST_123_')
