for wfile in *.whl
          do
            IFS=- read file_name  var1 <<< $wfile
            file_name=$(echo "$file_name" | tr '[:upper:]' '[:lower:]' | tr '_' '-')
            case "$(uname -sr)" in
              CYGWIN*|MINGW*|MINGW32*|MSYS*)
                if [[ "$file_name" == "esptool" ]]; then
                  continue
                fi
                ;;
              *)
                ;;
            esac
            aws s3 cp --acl=public-read --no-progress $wfile s3://$1/pypi/$file_name/$wfile
done