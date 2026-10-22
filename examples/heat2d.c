/* SPDX-License-Identifier: MIT
 * Fixed-work, double-precision diffusion stencil for execution-policy experiments.
 * This teaching workload is original to simtune, not a validated physics package.
 * Build: cc -O3 -std=c11 -fopenmp examples/heat2d.c -lm -o build/heat2d
 * Omit -fopenmp for a serial correctness smoke test. Do not compare different builds.
 */
#define _POSIX_C_SOURCE 200809L
#include <ctype.h>
#include <errno.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#ifdef _OPENMP
#include <omp.h>
#endif

static void usage(FILE *stream) {
    fprintf(stream, "Usage: heat2d [--size N] [--steps N] [--wave N] "
                    "[--first-touch parallel|serial] [--output-dir DIR]\n"
                    "Defaults: size=512 steps=100 wave=1 first-touch=parallel.\n"
                    "Threads are controlled by OpenMP environment variables.\n");
}

static int parse_positive(const char *text, size_t maximum, size_t *result) {
    if (!*text) return 0;
    for (const char *p = text; *p; ++p) {
        if (!isdigit((unsigned char)*p)) return 0;
    }
    errno = 0;
    char *end;
    unsigned long long value = strtoull(text, &end, 10);
    if (errno || *end || !value || value > maximum) return 0;
    *result = (size_t)value;
    return 1;
}

static double monotonic_seconds(void) {
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) {
        perror("clock_gettime");
        exit(1);
    }
    return (double)now.tv_sec + (double)now.tv_nsec / 1e9;
}

int main(int argc, char **argv) {
    size_t n = 512, steps = 100, wave = 1;
    int parallel_touch = 1;
    const char *output_dir = NULL;
    for (int arg = 1; arg < argc; ++arg) {
        if (strcmp(argv[arg], "--help") == 0) {
            usage(stdout);
            return 0;
        }
        if (arg + 1 >= argc) {
            fprintf(stderr, "Missing value for %s\n", argv[arg]);
            return 2;
        }
        const char *option = argv[arg], *value = argv[++arg];
        int valid = 0;
        if (strcmp(option, "--size") == 0) valid = parse_positive(value, 32768, &n);
        else if (strcmp(option, "--steps") == 0) valid = parse_positive(value, 1000000, &steps);
        else if (strcmp(option, "--wave") == 0) valid = parse_positive(value, 32766, &wave);
        else if (strcmp(option, "--first-touch") == 0) {
            valid = strcmp(value, "serial") == 0 || strcmp(value, "parallel") == 0;
            parallel_touch = strcmp(value, "parallel") == 0;
        }
        else if (strcmp(option, "--output-dir") == 0) {
            valid = *value != '\0';
            output_dir = value;
        }
        if (!valid) {
            fprintf(stderr, "Invalid option or value: %s %s\n", option, value);
            return 2;
        }
    }
    if (n < 3 || wave >= n - 1 || n > SIZE_MAX / n / sizeof(double) / 2) {
        fprintf(stderr, "Require size >= 3, wave < size - 1, and representable allocation\n");
        return 2;
    }

    const size_t cells = n * n;
    double *a = malloc(cells * sizeof(*a));
    double *b = malloc(cells * sizeof(*b));
    double *shape = malloc(n * sizeof(*shape));
    if (!a || !b || !shape) {
        fprintf(stderr, "Cannot allocate two %zux%zu grids (%zu bytes plus small overhead)\n",
                n, n, 2 * cells * sizeof(double));
        free(a); free(b); free(shape);
        return 1;
    }

    const double pi = acos(-1.0), alpha = 0.2;
    for (size_t i = 0; i < n; ++i) {
        shape[i] = (i == 0 || i == n - 1) ? 0.0 : sin(pi * (double)wave * i / (n - 1));
    }
    const double init_start = monotonic_seconds();
    /* Touch both buffers with the declared initialization policy. This choice
     * is part of the fixed workload, never a setting to vary within an A/B pair. */
#ifdef _OPENMP
#pragma omp parallel for schedule(static) if(parallel_touch)
#endif
    for (size_t i = 0; i < n; ++i) {
        for (size_t j = 0; j < n; ++j) {
            a[i * n + j] = shape[i] * shape[j];
            b[i * n + j] = 0.0;
        }
    }
    const double init_seconds = monotonic_seconds() - init_start;

    int actual_threads = 1;
    const double kernel_start = monotonic_seconds();
#ifdef _OPENMP
#pragma omp parallel shared(a, b, actual_threads)
#endif
    {
#ifdef _OPENMP
#pragma omp single
        actual_threads = omp_get_num_threads();
#endif
        for (size_t step = 0; step < steps; ++step) {
#ifdef _OPENMP
#pragma omp for schedule(static)
#endif
            for (size_t i = 1; i < n - 1; ++i) {
                for (size_t j = 1; j < n - 1; ++j) {
                    const size_t p = i * n + j;
                    b[p] = a[p] + alpha * (a[p - n] + a[p + n] + a[p - 1] + a[p + 1]
                                           - 4.0 * a[p]);
                }
            }
            /* The for barrier completes writes; the single barrier publishes
             * the pointer swap to all threads before the next step. */
#ifdef _OPENMP
#pragma omp single
#endif
            {
                double *swap = a;
                a = b;
                b = swap;
            }
        }
    }
    const double kernel_seconds = monotonic_seconds() - kernel_start;

    /* The initial sine mode is an eigenvector of the discrete update operator.
     * Check that exact discrete solution, not a continuum approximation. */
    const double s = sin(pi * (double)wave / (2.0 * (n - 1)));
    const double decay = pow(1.0 - 8.0 * alpha * s * s, (double)steps);
    double max_error = 0.0, checksum = 0.0;
    int finite = 1;
    for (size_t i = 0; i < n; ++i) {
        for (size_t j = 0; j < n; ++j) {
            const double value = a[i * n + j];
            const double error = fabs(value - decay * shape[i] * shape[j]);
            if (!isfinite(value) || !isfinite(error)) finite = 0;
            if (error > max_error) max_error = error;
            checksum += value;
        }
    }
    const double tolerance = 1e-9;
    const int valid = finite && isfinite(checksum) && max_error <= tolerance;
#ifdef _OPENMP
    const int openmp = _OPENMP;
#else
    const int openmp = 0;
#endif
    /* Serialize once: optional trial file and legacy stdout are byte-identical. */
    char checksum_text[64], error_text[64], result[2048];
    if (finite && isfinite(checksum)) snprintf(checksum_text, sizeof(checksum_text), "%.17g", checksum);
    else strcpy(checksum_text, "null");
    if (finite) snprintf(error_text, sizeof(error_text), "%.17g", max_error);
    else strcpy(error_text, "null");
    int length = snprintf(result, sizeof(result),
           "{\"workload\":\"heat2d\",\"schema_version\":1,\"size\":%zu,\"steps\":%zu,"
           "\"wave\":%zu,\"first_touch\":\"%s\",\"openmp\":%d,\"threads\":%d,"
           "\"grid_bytes\":%zu,\"updates\":%.0f,\"init_seconds\":%.9f,"
           "\"kernel_seconds\":%.9f,\"checksum\":%s,\"max_abs_error\":%s,"
           "\"tolerance\":%.17g,\"valid\":%s}\n",
           n, steps, wave, parallel_touch ? "parallel" : "serial", openmp, actual_threads,
           2 * cells * sizeof(double), (double)(n - 2) * (n - 2) * steps,
           init_seconds, kernel_seconds, checksum_text, error_text, tolerance, valid ? "true" : "false");
    int output_ok = length >= 0 && (size_t)length < sizeof(result);
    if (output_ok && output_dir) {
        const char *suffix = "/heat-result.json";
        size_t dir_length = strlen(output_dir);
        char *path = dir_length <= SIZE_MAX - strlen(suffix) - 1
            ? malloc(dir_length + strlen(suffix) + 1) : NULL;
        if (!path) {
            fprintf(stderr, "Cannot allocate output path\n");
            output_ok = 0;
        } else {
            strcpy(path, output_dir);
            strcat(path, suffix);
            FILE *stream = fopen(path, "w");
            if (!stream) {
                perror("heat-result.json");
                output_ok = 0;
            } else {
                int written = fwrite(result, 1, (size_t)length, stream) == (size_t)length;
                int closed = fclose(stream) == 0;
                if (!written || !closed) {
                    fprintf(stderr, "Cannot write heat-result.json\n");
                    output_ok = 0;
                }
            }
            free(path);
        }
    }
    if (length >= 0 && (size_t)length < sizeof(result)) {
        if (fwrite(result, 1, (size_t)length, stdout) != (size_t)length || fflush(stdout) != 0) {
            fprintf(stderr, "Cannot write heat result to stdout\n");
            output_ok = 0;
        }
    } else fprintf(stderr, "Cannot serialize heat result\n");
    free(a); free(b); free(shape);
    return valid && output_ok ? 0 : 1;
}
