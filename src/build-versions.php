<?php

declare(strict_types=1);

// Query the built image itself. Never infer installed versions from release feeds.
function commandOutput(string $command): string
{
    $lines = [];
    $status = 0;
    exec($command . ' 2>&1', $lines, $status);
    if ($status !== 0) {
        throw new RuntimeException('Version command failed: ' . $command . "\n" . implode("\n", $lines));
    }

    return trim(implode("\n", $lines));
}

function toolVersion(string $command, string $pattern): string
{
    $output = commandOutput($command);
    if (preg_match($pattern, $output, $matches) !== 1) {
        throw new RuntimeException('Cannot parse version from: ' . $output);
    }

    return $matches[1];
}

function nativePackageVersion(string $package): string
{
    $output = commandOutput(
        "dpkg-query -W -f='\${db:Status-Status}\\t\${Version}' " . escapeshellarg($package)
    );
    [$state, $version] = array_pad(explode("\t", $output, 2), 2, '');
    if ($state !== 'installed' || trim($version) === '') {
        throw new RuntimeException('Native package is not installed or has no version: ' . $package);
    }

    return $version;
}

try {
    require_once '/usr/local/lib/php-versions.php';
    assertSupportedPhpSeries(PHP_MAJOR_VERSION . '.' . PHP_MINOR_VERSION);

    $required = [
        'apcu', 'bcmath', 'bz2', 'exif', 'gd', 'gmp', 'imagick', 'intl',
        'mbstring', 'memcached', 'mysqli', 'mysqlnd', 'pcntl', 'pdo_mysql',
        'pdo_pgsql', 'rdkafka', 'redis', 'sysvsem', 'zip', 'Zend OPcache',
    ];
    foreach ($required as $name) {
        if (!extension_loaded($name)) {
            throw new RuntimeException('Required extension is not loaded: ' . $name);
        }
    }

    $extensions = [];
    foreach (get_loaded_extensions() as $name) {
        $key = $name === 'Zend OPcache' ? 'opcache' : $name;
        $extensions[$key] = (new ReflectionExtension($name))->getVersion();
    }
    ksort($extensions, SORT_NATURAL | SORT_FLAG_CASE);

    $libraries = [
        'gmp' => GMP_VERSION,
        'icu' => INTL_ICU_VERSION,
        'imagemagick' => Imagick::getVersion()['versionString'],
        'libxml' => LIBXML_DOTTED_VERSION,
        'libzip' => ZipArchive::LIBZIP_VERSION,
        'openssl' => OPENSSL_VERSION_TEXT,
        'pcre' => PCRE_VERSION,
        'zlib' => ZLIB_VERSION,
    ];
    if (defined('INTL_ICU_DATA_VERSION')) {
        $libraries['icu_data'] = INTL_ICU_DATA_VERSION;
    }
    $gd = gd_info();
    $libraries['gd'] = $gd['GD Version'];

    // Debian revisions identify the actual native libraries used during compilation.
    $packages = [
        'libbz2-dev', 'libfreetype-dev', 'libgmp-dev', 'libicu-dev',
        'libjpeg62-turbo-dev', 'libmagickwand-dev', 'libmemcached-dev',
        'libonig-dev', 'libpng-dev', 'libpq-dev', 'librdkafka-dev',
        'libsasl2-dev', 'libxml2-dev', 'libzip-dev', 'zlib1g-dev',
    ];
    $nativePackages = [];
    foreach ($packages as $package) {
        $nativePackages[$package] = nativePackageVersion($package);
    }
    ksort($libraries);

    echo json_encode([
        'php' => PHP_VERSION,
        'extensions' => $extensions,
        'tools' => [
            'composer' => toolVersion('composer --version --no-ansi', '/Composer version (\S+)/'),
            'nginx' => toolVersion('nginx -v', '/nginx\/([^\s]+)/'),
            'node' => toolVersion('node --version', '/^v([^\s]+)/'),
            'npm' => toolVersion('npm --version', '/^([0-9]+\.[0-9]+\.[^\s]+)/m'),
        ],
        'libraries' => $libraries,
        'native_packages' => $nativePackages,
    ], JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR), "\n";
} catch (Throwable $error) {
    fwrite(STDERR, $error->getMessage() . "\n");
    exit(1);
}
