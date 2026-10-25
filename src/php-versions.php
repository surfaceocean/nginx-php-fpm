<?php

declare(strict_types=1);

function supportedPhpVersions(string $path = '/usr/local/share/php-versions.json'): array
{
    if (!is_readable($path)) {
        throw new RuntimeException('PHP version configuration must be readable: ' . $path);
    }
    $source = file_get_contents($path);
    if ($source === false) {
        throw new RuntimeException('Cannot read PHP version configuration: ' . $path);
    }
    $versions = json_decode($source, false, 512, JSON_THROW_ON_ERROR);
    if (!is_array($versions) || !array_is_list($versions) || $versions === []) {
        throw new RuntimeException('PHP version configuration must be a nonempty JSON array.');
    }
    foreach ($versions as $series) {
        if (!is_string($series) || preg_match('/\A[1-9][0-9]*\.(?:0|[1-9][0-9]*)\z/', $series) !== 1) {
            throw new RuntimeException('PHP versions must use major.minor without leading zeroes.');
        }
    }
    if (count(array_unique($versions, SORT_STRING)) !== count($versions)) {
        throw new RuntimeException('PHP version configuration contains duplicate versions.');
    }
    usort($versions, 'version_compare');

    return $versions;
}

function assertSupportedPhpSeries(string $series, string $path = '/usr/local/share/php-versions.json'): void
{
    $versions = supportedPhpVersions($path);
    if (!in_array($series, $versions, true)) {
        throw new RuntimeException('Unsupported PHP version: ' . $series . '; configured versions: ' . implode(', ', $versions));
    }
}
