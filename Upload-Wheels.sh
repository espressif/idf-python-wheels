#! /bin/bash

cd download

for wfile in download/*.whl
          do 
            IFS=- read file_name  var1 <<< $wfile
            file_name=$(echo "$file_name" | tr '[:upper:]' '[:lower:]')
            echo $file_name
            aws s3 cp --acl=public-read --no-progress $wfile s3://$1/pypi/$file_name/$wfile
done
