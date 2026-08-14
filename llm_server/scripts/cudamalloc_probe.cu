/* cudamalloc_probe: пытается выделить N гигабайт одним непрерывным cudaMalloc.
 *
 * Проверяет конкретный регресс на L4T 36.4.7 (сломанный аллокатор крупных
 * непрерывных CUDA-буферов, NvMap) — см. iros_llm_server_JETSON_UPDATE.md,
 * задача 1. Только stdlib + CUDA runtime, без сторонних зависимостей:
 * это диагностика платформы, должна собираться и на голом Jetson без venv
 * или пакетов помимо CUDA toolkit.
 *
 * Usage: cudamalloc_probe <size_gb>
 * Exit code: 0 при успехе, 1 при ошибке cudaMalloc.
 */
#include <cstdio>
#include <cstdlib>
#include <cuda_runtime.h>

int main(int argc, char **argv) {
  if (argc != 2) {
    std::fprintf(stderr, "Usage: %s <size_gb>\n", argv[0]);
    return 1;
  }

  double size_gb = std::atof(argv[1]);
  if (size_gb <= 0.0) {
    std::fprintf(stderr, "error: size_gb must be > 0, got '%s'\n", argv[1]);
    return 1;
  }

  size_t size_bytes = static_cast<size_t>(size_gb * 1024.0 * 1024.0 * 1024.0);

  void *ptr = nullptr;
  cudaError_t err = cudaMalloc(&ptr, size_bytes);

  if (err != cudaSuccess) {
    std::printf("%.2f GB: FAIL (%s)\n", size_gb, cudaGetErrorString(err));
    return 1;
  }

  std::printf("%.2f GB: OK (%s)\n", size_gb, cudaGetErrorString(err));
  cudaFree(ptr);
  return 0;
}
