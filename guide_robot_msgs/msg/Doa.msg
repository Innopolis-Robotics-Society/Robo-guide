# Направление на источник звука.

std_msgs/Header header

# Азимут, рад, против часовой стрелки от оси +X кадра mic_array.
float32 azimuth

# Энергия выбранного луча относительно остальных, дБ.
# Косвенная мера уверенности: у XVF3800 нет явного confidence.
float32 beam_snr_db

bool voice_active
